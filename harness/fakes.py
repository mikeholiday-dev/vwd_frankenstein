"""Stand-ins so each workstream can run end-to-end before the others land.

DEV ONLY. `FRANK_MODE=demo` refuses all of these (see config.py). Each fake
honours its Protocol in contracts.py, so swapping in the real one is a
one-line change in wiring.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from harness import config
from harness.contracts import (
    REGISTRY_ENV,
    ApprovalDecision,
    ApprovalRequest,
    EventType,
    Manifest,
    Phase,
    RegistryEntry,
    SandboxResult,
)
from harness.kernel.limits import MAX_SANDBOX_SECONDS
from harness.ops.events import EventLog


class LocalSandbox:
    """Runs code in a host subprocess with a scrubbed env: no API key, no network
    isolation, no filesystem isolation, so code can still read your Claude Code
    login under ~/.claude. Fine for team-written fixtures; for agent-written
    code switch to the Docker sandbox as soon as it exists. No gateway either:
    `secrets` are ignored, so a keyed request goes out without its key.
    """

    def run(self, workdir, argv, *, phase, network=(), deps=(), stdin=None, timeout_s=None, registry_ro=False, secrets=()):
        assert config.MODE != "demo", "LocalSandbox is dev-only"
        Phase(phase)
        env = {"PATH": os.environ["PATH"], "HOME": str(workdir), "UV_CACHE_DIR": str(config.ROOT / ".cache" / "uv")}
        if registry_ro:
            env[REGISTRY_ENV] = str(config.REGISTRY_DIR)
        if argv and argv[0] == "python":
            withs = [x for d in ["pytest", *deps] for x in ("--with", d)]
            argv = ["uv", "run", "--quiet", "--no-project", "--python", f"{sys.version_info.major}.{sys.version_info.minor}", *withs, *argv]
        t0 = time.monotonic()
        try:
            p = subprocess.run(argv, cwd=workdir, env=env, input=stdin, capture_output=True, text=True, timeout=timeout_s or MAX_SANDBOX_SECONDS)
            return SandboxResult(p.returncode, p.stdout, p.stderr, time.monotonic() - t0, run_id=uuid.uuid4().hex[:8])
        except subprocess.TimeoutExpired as e:
            return SandboxResult(-1, e.stdout or "", e.stderr or "", time.monotonic() - t0, timed_out=True, run_id=uuid.uuid4().hex[:8])


class DirRegistry:
    """Plain folders, no git: <root>/<name>/v<N>/ plus <root>/_state.json."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._state_path = root / "_state.json"

    def _state(self) -> dict:
        return json.loads(self._state_path.read_text()) if self._state_path.exists() else {}

    def _save(self, state: dict) -> None:
        self._state_path.write_text(json.dumps(state, indent=2))

    def _entry(self, name: str, version: int, s: dict) -> RegistryEntry:
        path = self.root / name / f"v{version}"
        return RegistryEntry(Manifest.load(path), path, "quarantined" if s["quarantined"] else "active", s["installed_at"][str(version)])

    def list(self, include_quarantined=False):
        return [self._entry(n, s["active"], s) for n, s in sorted(self._state().items()) if include_quarantined or not s["quarantined"]]

    def get(self, name, version=None):
        s = self._state()[name]
        return self._entry(name, version or s["active"], s)

    def install(self, bundle_dir, manifest):
        dest = self.root / manifest.name / f"v{manifest.version}"
        if dest.exists():
            raise FileExistsError(f"{manifest.ref} already installed")
        shutil.copytree(bundle_dir, dest)
        state = self._state()
        s = state.setdefault(manifest.name, {"active": manifest.version, "quarantined": False, "installed_at": {}})
        s.update(active=manifest.version, quarantined=False)
        s["installed_at"][str(manifest.version)] = datetime.now(UTC).isoformat(timespec="seconds")
        self._save(state)
        return self.get(manifest.name)

    def rollback(self, name, version):
        state = self._state()
        if str(version) not in state[name]["installed_at"]:
            raise KeyError(f"{name}@v{version} was never installed")
        state[name]["active"] = version
        self._save(state)
        return self.get(name)

    def quarantine(self, name):
        state = self._state()
        state[name]["quarantined"] = True
        self._save(state)


class AutoApprover:
    """Approves whatever passed its tests. For unattended dev loops only."""

    def __init__(self, events: EventLog):
        self.events = events

    def request(self, req: ApprovalRequest) -> ApprovalDecision:
        assert config.MODE != "demo", "AutoApprover is dev-only"
        self.events.emit(EventType.APPROVAL_REQUESTED, **vars(req))
        d = ApprovalDecision(req.id, req.test_report.passed, "auto-dev")
        self.events.emit(EventType.APPROVAL_DECIDED, **vars(d))
        return d


class CliApprover:
    """Asks on the terminal. Lets streams A and B work without the UI."""

    def __init__(self, events: EventLog):
        self.events = events

    def request(self, req: ApprovalRequest) -> ApprovalDecision:
        self.events.emit(EventType.APPROVAL_REQUESTED, **vars(req))
        print(f"\n=== approve {req.ref}? tests {'PASSED' if req.test_report.passed else 'FAILED'}")
        print(f"permissions diff: {json.dumps(req.permissions_diff)}")
        print(f"description: {req.manifest['description']}")
        answer = input("approve? [y/N/c=show code] ").strip().lower()
        if answer == "c":
            print(req.code)
            answer = input("approve? [y/N] ").strip().lower()
        d = ApprovalDecision(req.id, answer == "y", "cli")
        self.events.emit(EventType.APPROVAL_DECIDED, **vars(d))
        return d
