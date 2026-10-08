"""Capability host: calls installed capabilities in the sandbox. Owner: A.

Each call copies the active version to a fresh dir, drops the call shim next to
it, and runs it in the sandbox with only the manifest's domains allowed. The
capabilities it `uses` are resolved again on every call (compose.py) and copied
alongside; the `call` event lists them in `uses`.

`attach` makes the operator's files for this session (task 2's invoice PDF)
readable by every call, read-only, at `_frank_inputs/<name>` relative to the
call's working dir. The agent passes that path as an argument. Test runs don't
see them: stored tests must not depend on one session's files.

`retest` re-runs an installed version's stored tests: the primitive behind
capability_doctor (stream B wraps it as a kernel tool).

TODO(A): prompt_skill calls.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from harness.contracts import CALL_SHIM_FILE, CallResult, EventType, Phase, Registry, Sandbox, TestReport
from harness.kernel.compose import UsesError, dependencies, resolve, vendor
from harness.kernel.gate import record_test, run_tests
from harness.ops.events import EventLog

INPUTS_DIR = "_frank_inputs"

CALL_SHIM = """\
import json, sys, traceback
try:
    from capability import run
    out = run(**json.loads(sys.stdin.read())["args"])
    print(json.dumps({"ok": True, "output": out}, ensure_ascii=False, default=str))
except Exception as e:
    print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-2000:]}))
"""


class Host:
    def __init__(self, sandbox: Sandbox, registry: Registry, events: EventLog):
        self.sandbox, self.registry, self.events = sandbox, registry, events
        self.inputs: dict[str, Path] = {}

    def attach(self, *paths: Path) -> list[str]:
        """Hand the operator's files to every later call. Returns the paths a capability opens them at."""
        for p in map(Path, paths):
            if not p.is_file():
                raise FileNotFoundError(f"{p} is not a file")
            if p.name in self.inputs and self.inputs[p.name] != p.resolve():
                raise ValueError(f"two inputs named {p.name}")
            self.inputs[p.name] = p.resolve()
        return [f"{INPUTS_DIR}/{n}" for n in self.inputs]

    def available(self):
        return self.registry.list()

    def call(self, name: str, args: dict[str, Any], version: int | None = None) -> CallResult:
        entry = self.registry.get(name, version)
        m = entry.manifest
        call_id = f"call-{uuid.uuid4().hex[:8]}"
        if entry.status != "active":
            return self._log(CallResult(call_id, m.ref, False, error=f"{m.ref} is {entry.status}"), args, [])
        try:
            uses = resolve(self.registry, m)
        except UsesError as e:
            return self._log(CallResult(call_id, m.ref, False, error=str(e)), args, [])
        with tempfile.TemporaryDirectory(prefix=f"call-{name}-") as tmp:
            work = Path(tmp) / "cap"
            shutil.copytree(entry.path, work)
            vendor(uses, work)
            shutil.rmtree(work / INPUTS_DIR, ignore_errors=True)
            if self.inputs:
                (work / INPUTS_DIR).mkdir()
                for n, src in self.inputs.items():
                    shutil.copyfile(src, work / INPUTS_DIR / n)
            (work / CALL_SHIM_FILE).write_text(CALL_SHIM)
            r = self.sandbox.run(
                work, ["python", CALL_SHIM_FILE], phase=Phase.CALL, network=m.permissions.network,
                deps=dependencies(m, uses), stdin=json.dumps({"args": args}), registry_ro=m.permissions.filesystem == "registry_ro",
            )
        try:
            out = json.loads(r.stdout.strip().splitlines()[-1])
            result = CallResult(call_id, m.ref, out["ok"], out.get("output"), out.get("error", ""), round(r.duration_s, 2))
        except (IndexError, json.JSONDecodeError, KeyError):
            result = CallResult(call_id, m.ref, False, error=f"no result (exit {r.exit_code}): {r.stderr[-2000:]}", duration_s=round(r.duration_s, 2))
        return self._log(result, args, r.egress_denied, [u.manifest.ref for u in uses])

    def retest(self, name: str, version: int | None = None) -> TestReport:
        """Re-run the stored tests of an installed version (default: active), quarantined or not. Logged as test_run."""
        entry = self.registry.get(name, version)
        try:
            uses = resolve(self.registry, entry.manifest)
        except UsesError as e:
            report = TestReport(entry.manifest.ref, False, -1, f"[uses] {e}", 0.0, "")
            self.events.emit(EventType.TEST_RUN, **vars(report), suite="retest", egress_denied=[])
        else:
            report = run_tests(self.sandbox, entry.path, entry.manifest, self.events, suite="retest", uses=uses)
        record_test(self.registry, entry.manifest, report.passed, "retest")
        return report

    def _log(self, result: CallResult, args: dict[str, Any], egress_denied: list[str], uses: list[str] = ()) -> CallResult:
        self.events.emit(EventType.CALL, **vars(result), args=args, egress_denied=egress_denied, uses=list(uses))
        return result
