"""Docker sandbox (plan §6). Owner: A.

Implements contracts.Sandbox. Until it's done, wiring uses fakes.LocalSandbox.
Drop-in check: `FRANK_FAKE=registry uv run pytest tests/test_install_flow.py`.

Requirements:
- one `docker run --rm` per call; image with python 3.12 + uv + pytest (see Dockerfile)
- mount `workdir` (read-write for build/test, read-only for call); no other host mounts,
  except the registry read-only at /registry when `registry_ro` (and set REGISTRY_ENV=/registry)
- env: nothing from the host. No ANTHROPIC_API_KEY, ever. No home-dir mounts either:
  we run on Claude Code subscription logins, so ~/.claude and ~/.config hold credentials
- network: only via the egress proxy. Register a per-container token with
  proxy.allow(token, network) and set HTTPS_PROXY=http://<token>@<proxy>:<port>.
  BUILD phase → package index only; TEST/CALL → manifest domains only
- --cpus, --memory, --pids-limit, and `timeout_s` (default limits.MAX_SANDBOX_SECONDS); kill on timeout
- `deps`: install with uv; cache per (deps hash) so calls stay fast
"""

from __future__ import annotations

from harness.contracts import SandboxResult


class DockerSandbox:
    def run(self, workdir, argv, *, phase, network=(), deps=(), stdin=None, timeout_s=None, registry_ro=False) -> SandboxResult:
        raise NotImplementedError("stream A")
