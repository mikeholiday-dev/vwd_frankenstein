"""Docker sandbox (plan §6). Owner: A.

Implements contracts.Sandbox: one `docker run --rm` per call, from the image in
sandbox.Dockerfile (built on first use, tagged with the file's hash).

- mounts: `workdir` at /work (read-write for build/test, read-only for call), the deps
  volume read-only at /deps, and the registry read-only at /registry when `registry_ro`
  (REGISTRY_ENV=/registry). No other host mounts: no home dir, so no ~/.claude login
- env: nothing from the host, so never ANTHROPIC_API_KEY. Runs as the host uid, so the
  bind-mounted workdir is writable on Linux too, with a read-only root fs and a /tmp tmpfs
- network: only through the egress proxy (proxy.py). The container joins an internal
  docker network whose one way out is the proxy, with a per-container token allowing
  BUILD (and deps installs) → package index only, TEST/CALL → the manifest's domains only.
  Refused hosts come back in `egress_denied` and as `[egress]` lines on stderr
- limits: --cpus, --memory, --pids-limit, no capabilities, and `timeout_s`
  (default limits.MAX_SANDBOX_SECONDS); on timeout the container is killed
- `deps`: installed with uv into a named volume per deps hash, so calls stay fast

Drop-in check: `FRANK_FAKE=registry uv run pytest tests/test_install_flow.py`.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from harness import config
from harness.contracts import REGISTRY_ENV, Phase, SandboxResult
from harness.kernel.limits import MAX_SANDBOX_SECONDS
from harness.kernel.proxy import PACKAGE_INDEX, EgressProxy

DOCKERFILE = Path(__file__).with_name("sandbox.Dockerfile")
# Docker Desktop on macOS links the CLI here, but not every shell has it on PATH.
DOCKER_FALLBACKS = ("/usr/local/bin/docker", "/Applications/Docker.app/Contents/Resources/bin/docker")
CONFINEMENT = [
    "--cpus", "1", "--memory", "1g", "--pids-limit", "256",
    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
    "--read-only", "--tmpfs", "/tmp:rw,exec,nosuid,size=256m",
]  # fmt: skip


class DockerUnavailable(RuntimeError):
    """No docker CLI or daemon on this machine."""


class DockerSandbox:
    _built: set[str] = set()  # images known to exist, per process
    _deps_ready: set[str] = set()  # deps volumes known to be complete, per process

    def __init__(self, registry_dir: Path | None = None, state_dir: Path | None = None):
        self.registry_dir = registry_dir or config.REGISTRY_DIR
        docker = shutil.which("docker") or next((p for p in DOCKER_FALLBACKS if os.access(p, os.X_OK)), None)
        if not docker:
            raise DockerUnavailable("docker CLI not found; install Docker Desktop or use FRANK_FAKE=sandbox,registry in dev")
        self.docker = docker
        # Host env goes to the docker CLI only (it needs its credential helpers next to it), never into a container.
        self._cli_env = {**os.environ, "PATH": f"{Path(docker).parent}{os.pathsep}{os.environ.get('PATH', '')}"}
        self.image = f"frank-sandbox:{hashlib.sha256(DOCKERFILE.read_bytes()).hexdigest()[:12]}"
        self.user = f"{os.getuid()}:{os.getgid()}"
        self._ensure_image()
        self.proxy = EgressProxy(docker, self._cli_env, self.image, self.user, state_dir or config.ROOT / ".cache" / "egress")
        self.proxy.ensure()

    def run(self, workdir, argv, *, phase, network=(), deps=(), stdin=None, timeout_s=None, registry_ro=False) -> SandboxResult:
        phase = Phase(phase)
        run_id = uuid.uuid4().hex[:8]
        t0 = time.monotonic()
        flags = ["-v", f"{Path(workdir).resolve()}:/work:{'ro' if phase == Phase.CALL else 'rw'}"]
        if deps:
            volume, failed = self._deps(list(deps), run_id)
            if failed:
                failed.duration_s = time.monotonic() - t0
                return failed
            flags += ["-v", f"{volume}:/deps:ro", "-e", "PYTHONPATH=/deps"]
        if registry_ro:
            flags += ["-v", f"{self.registry_dir.resolve()}:/registry:ro", "-e", f"{REGISTRY_ENV}=/registry"]
        domains = PACKAGE_INDEX if phase == Phase.BUILD else list(network)
        return self._container(run_id, phase, flags, list(argv), stdin, timeout_s or MAX_SANDBOX_SECONDS, domains)

    def _deps(self, deps: list[str], run_id: str) -> tuple[str, SandboxResult | None]:
        """Install `deps` once into a volume named after their hash. Returns (volume, None) or ("", failure)."""
        if bad := [d for d in deps if not d or d.startswith("-")]:
            return "", SandboxResult(2, "", f"[deps] refused, not package specs: {bad}\n", 0.0, run_id=run_id)
        volume = "frank-deps-" + hashlib.sha256("\n".join(sorted(deps)).encode()).hexdigest()[:16]
        if volume in self._deps_ready:
            return volume, None
        script = (
            "test -f /deps/.frank-ok && exit 0; find /deps -mindepth 1 -delete; "
            'uv pip install -q --python python --target /deps "$@" && touch /deps/.frank-ok'
        )
        r = self._container(
            run_id, Phase.BUILD, ["-v", f"{volume}:/deps", "-e", "UV_CACHE_DIR=/tmp/uv"],
            ["sh", "-c", script, "deps", *deps], None, MAX_SANDBOX_SECONDS, PACKAGE_INDEX,
        )
        if r.exit_code != 0 or r.timed_out:
            r.stderr = f"[deps] install of {deps} failed\n{r.stderr}"
            return "", r
        self._deps_ready.add(volume)
        return volume, None

    def _container(
        self, run_id: str, phase: Phase, flags: list[str], argv: list[str], stdin: str | None, timeout_s: int, domains: list[str]
    ) -> SandboxResult:
        name = f"frank-{phase}-{run_id}"
        token = secrets.token_hex(16)
        cmd = [
            self.docker, "run", "--rm", *(["-i"] if stdin is not None else []), "--name", name, "--label", f"frank.phase={phase}",
            "--user", self.user, "-e", "HOME=/tmp", "-w", "/work", *CONFINEMENT, *self.proxy.env(token), *flags, self.image, *argv,
        ]  # fmt: skip
        io = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        self.proxy.allow(token, domains)
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, env=self._cli_env, **io)
            r = SandboxResult(p.returncode, p.stdout, p.stderr, time.monotonic() - t0, run_id=run_id)
        except subprocess.TimeoutExpired as e:
            subprocess.run([self.docker, "kill", name], capture_output=True, env=self._cli_env)
            r = SandboxResult(-1, _text(e.stdout), _text(e.stderr), time.monotonic() - t0, timed_out=True, run_id=run_id)
        finally:
            self.proxy.revoke(token)
        for e in self.proxy.take_log(token):
            if not e["allowed"]:
                r.egress_denied.append(f"{e['host']}:{e['port']}")
                r.stderr += f"[egress] refused {e['host']}:{e['port']}: {e['reason']}\n"
        return r

    def _ensure_image(self) -> None:
        if self.image in self._built:
            return
        try:
            p = subprocess.run([self.docker, "image", "inspect", self.image], capture_output=True, text=True, env=self._cli_env)
        except OSError as e:
            raise DockerUnavailable(str(e)) from e
        if p.returncode != 0:
            if "no such image" not in p.stderr.lower():
                raise DockerUnavailable(p.stderr.strip())  # daemon down, no permission, ...
            build = [self.docker, "build", "-q", "-t", self.image, "-"]
            b = subprocess.run(build, input=DOCKERFILE.read_text(), capture_output=True, text=True, env=self._cli_env)
            if b.returncode != 0:
                raise DockerUnavailable(f"building {self.image} failed:\n{b.stderr[-2000:]}")
        self._built.add(self.image)


def _text(out: str | bytes | None) -> str:
    return out.decode(errors="replace") if isinstance(out, bytes) else out or ""
