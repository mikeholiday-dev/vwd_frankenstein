"""Capability host: calls installed capabilities in the sandbox. Owner: A.

Each call copies the active version to a fresh dir, drops the call shim next to
it, and runs it in the sandbox with only the manifest's domains allowed.

TODO(A): prompt_skill calls; `uses` (a capability calling another one).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from harness.contracts import CALL_SHIM_FILE, CallResult, EventType, Phase, Registry, Sandbox
from harness.ops.events import EventLog

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

    def available(self):
        return self.registry.list()

    def call(self, name: str, args: dict[str, Any], version: int | None = None) -> CallResult:
        entry = self.registry.get(name, version)
        m = entry.manifest
        call_id = f"call-{uuid.uuid4().hex[:8]}"
        if entry.status != "active":
            return self._log(CallResult(call_id, m.ref, False, error=f"{m.ref} is {entry.status}"), args)
        with tempfile.TemporaryDirectory(prefix=f"call-{name}-") as tmp:
            work = Path(tmp) / "cap"
            shutil.copytree(entry.path, work)
            (work / CALL_SHIM_FILE).write_text(CALL_SHIM)
            r = self.sandbox.run(
                work, ["python", CALL_SHIM_FILE], phase=Phase.CALL, network=m.permissions.network,
                deps=m.dependencies, stdin=json.dumps({"args": args}), registry_ro=m.permissions.filesystem == "registry_ro",
            )
        try:
            out = json.loads(r.stdout.strip().splitlines()[-1])
            result = CallResult(call_id, m.ref, out["ok"], out.get("output"), out.get("error", ""), round(r.duration_s, 2))
        except (IndexError, json.JSONDecodeError, KeyError):
            result = CallResult(call_id, m.ref, False, error=f"no result (exit {r.exit_code}): {r.stderr[-2000:]}", duration_s=round(r.duration_s, 2))
        return self._log(result, args)

    def _log(self, result: CallResult, args: dict[str, Any]) -> CallResult:
        self.events.emit(EventType.CALL, **vars(result), args=args)
        return result
