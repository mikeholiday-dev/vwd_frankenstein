"""`frank` command line. Shared entry point.

  frank run --session A "task"     one agent session (one process; session B = run it again)
  frank install <bundle_dir>       push a hand-made bundle through the gate (kernel smoke test)
  frank call <name> '<json args>'  call an installed capability
  frank registry                   list installed capabilities
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from harness.contracts import EventType, to_jsonable
from harness.kernel.limits import CapExceeded
from harness.ops.approvals import Killed
from harness.wiring import build_context


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--session", default="dev")
    p = argparse.ArgumentParser(prog="frank")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", parents=[common])
    run.add_argument("task")
    inst = sub.add_parser("install", parents=[common])
    inst.add_argument("bundle", type=Path)
    call = sub.add_parser("call", parents=[common])
    call.add_argument("name")
    call.add_argument("args", nargs="?", default="{}")
    sub.add_parser("registry", parents=[common])
    a = p.parse_args(argv)

    ctx = build_context(a.session)
    if a.cmd == "registry":
        for e in ctx.registry.list(include_quarantined=True):
            print(f"{e.manifest.ref:32} {e.status:12} {', '.join(e.manifest.permissions.network) or '-'}")
        return 0
    if a.cmd == "install":
        print(json.dumps(to_jsonable(ctx.gate.submit(a.bundle)), indent=2, ensure_ascii=False))
        return 0
    if a.cmd == "call":
        print(json.dumps(to_jsonable(ctx.host.call(a.name, json.loads(a.args))), indent=2, ensure_ascii=False))
        return 0

    from harness.agent.loop import run_session

    ctx.events.emit(EventType.RUN_STARTED, task=a.task, pid=os.getpid(), registry=[e.manifest.ref for e in ctx.registry.list()])
    status = "failed"
    try:
        print(run_session(a.task, ctx))
        status = "ok"
    except Killed:
        status = "killed"
    except CapExceeded as e:
        status = "capped"
        print(e, file=sys.stderr)
    finally:
        ctx.events.emit(EventType.RUN_FINISHED, status=status, budget=ctx.budget.snapshot())
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
