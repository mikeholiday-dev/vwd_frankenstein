"""Git-backed registry (plan §3, §5). Owner: A.

Implements contracts.Registry with the same behaviour as fakes.DirRegistry
(tests/test_install_flow.py is the spec).

Layout: `registry/` is its own git repo.
- `<name>/` holds the *active* version's bundle, `_state.json` (tracked) holds the
  active version, quarantine flag and install times, as in DirRegistry
- one commit per install, tagged `<name>@v<N>`; rollback and quarantine are commits too
- install commits are authored by "frankenstein-agent" and name origin.task_id + session,
  so `git log` proves the team didn't write them
- get(name, version) materialises that version's tag into `.git/frank-cache/<name>@v<N>/`
  (tags never move, so the cache never goes stale); treat that folder as read-only
- writes take a file lock: the UI process rolls back and quarantines while the agent installs
"""

from __future__ import annotations

import fcntl
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from harness.contracts import Manifest, RegistryEntry

STATE_FILE = "_state.json"
GITIGNORE = "__pycache__/\n*.pyc\n.pytest_cache/\n"
AGENT = ("frankenstein-agent", "agent@frankenstein.invalid")
OPERATOR = ("frankenstein-operator", "operator@frankenstein.invalid")
HARNESS = ("frankenstein-harness", "harness@frankenstein.invalid")


class GitRegistry:
    def __init__(self, root: Path):
        self.root = root
        self._cache = root / ".git" / "frank-cache"
        if not (root / ".git").exists():
            if root.exists() and any(root.iterdir()):
                raise RuntimeError(f"{root} exists but isn't a git registry; move it aside (it may be a DirRegistry from dev)")
            root.mkdir(parents=True, exist_ok=True)
            self._git("init", "-q", "-b", "main")
            (root / ".gitignore").write_text(GITIGNORE)
            self._write_state({})
            self._commit(HARNESS, "registry created (empty)", ".gitignore", STATE_FILE)

    def list(self, include_quarantined=False):
        state = self._state()
        return [self._entry(n, s["active"], s) for n, s in sorted(state.items()) if include_quarantined or not s["quarantined"]]

    def get(self, name, version=None):
        s = self._state()[name]
        return self._entry(name, version or s["active"], s)

    def install(self, bundle_dir, manifest):
        with self._lock():
            if self._tag_exists(manifest.ref):
                raise FileExistsError(f"{manifest.ref} already installed")
            dest = self.root / manifest.name
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(bundle_dir, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
            state = self._state()
            s = state.setdefault(manifest.name, {"active": manifest.version, "quarantined": False, "installed_at": {}})
            s.update(active=manifest.version, quarantined=False)
            s["installed_at"][str(manifest.version)] = datetime.now(UTC).isoformat(timespec="seconds")
            self._write_state(state)
            o = manifest.origin
            msg = f"install {manifest.ref}\n\n{manifest.description}\n\ntask: {o.get('task_id', '?')}\nsession: {o.get('session', '?')}"
            if o.get("built_by"):
                msg += f"\nbuilt-by: {o['built_by']}"
            self._commit(AGENT, msg, manifest.name, STATE_FILE)
            self._git("tag", manifest.ref)
        return self.get(manifest.name)

    def rollback(self, name, version):
        with self._lock():
            state = self._state()
            if str(version) not in state[name]["installed_at"]:
                raise KeyError(f"{name}@v{version} was never installed")
            shutil.rmtree(self.root / name, ignore_errors=True)
            self._git("checkout", f"{name}@v{version}", "--", name)
            state[name]["active"] = version
            self._write_state(state)
            self._commit(OPERATOR, f"rollback {name} to v{version}", name, STATE_FILE)
        return self.get(name)

    def quarantine(self, name):
        with self._lock():
            state = self._state()
            state[name]["quarantined"] = True
            self._write_state(state)
            self._commit(OPERATOR, f"quarantine {name}", STATE_FILE)

    def _entry(self, name: str, version: int, s: dict) -> RegistryEntry:
        ref = f"{name}@v{version}"
        if str(version) not in s["installed_at"]:
            raise KeyError(f"{ref} was never installed")
        path = self._materialise(name, ref)
        return RegistryEntry(Manifest.load(path), path, "quarantined" if s["quarantined"] else "active", s["installed_at"][str(version)])

    def _materialise(self, name: str, ref: str) -> Path:
        path = self._cache / ref
        if path.exists():
            return path
        self._cache.mkdir(parents=True, exist_ok=True)
        archive = ["git", "archive", "--format=tar", f"refs/tags/{ref}", name]
        tar = subprocess.run(archive, cwd=self.root, capture_output=True, check=True).stdout
        tmp = Path(tempfile.mkdtemp(dir=self._cache, prefix=".tmp-"))
        with tarfile.open(fileobj=io.BytesIO(tar)) as t:
            t.extractall(tmp, filter="data")
        try:
            (tmp / name).rename(path)
        except OSError:  # another process materialised it first
            if not path.exists():
                raise
        shutil.rmtree(tmp, ignore_errors=True)
        return path

    def _state(self) -> dict:
        p = self.root / STATE_FILE
        return json.loads(p.read_text()) if p.exists() else {}

    def _write_state(self, state: dict) -> None:
        (self.root / STATE_FILE).write_text(json.dumps(state, indent=2) + "\n")

    def _tag_exists(self, ref: str) -> bool:
        verify = ["git", "rev-parse", "-q", "--verify", f"refs/tags/{ref}"]
        return subprocess.run(verify, cwd=self.root, capture_output=True).returncode == 0

    def _commit(self, who: tuple[str, str], message: str, *paths: str) -> None:
        self._git("add", "-A", "--", *paths)
        name, email = who
        env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": HARNESS[0], "GIT_COMMITTER_EMAIL": HARNESS[1]}
        self._git("commit", "-q", "--allow-empty", "-m", message, env=env)

    def _git(self, *args: str, env: dict[str, str] | None = None) -> str:
        # Ignore the user's hooks and signing setup: the registry's history must not depend on whose laptop ran it.
        cmd = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args]
        p = subprocess.run(cmd, cwd=self.root, capture_output=True, text=True, env={**os.environ, **(env or {})})
        if p.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
        return p.stdout

    @contextmanager
    def _lock(self):
        with open(self.root / ".git" / "frank.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
