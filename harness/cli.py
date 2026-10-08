"""`frank` command line. Shared entry point.

  frank run --session A "task"     one agent session (one process; session B = run it again)
  frank run --attach FILE "task"   ...with an input file capability calls can read (repeatable)
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

from harness import config
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
    run.add_argument("--attach", type=Path, action="append", default=[], metavar="FILE", help="input file for this session's capability calls (repeatable)")
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
    if a.cmd in ("install", "call"):  # a run of its own in the log, so it has to finish like one
        status = "failed"
        try:
            if a.cmd == "install":
                result = ctx.gate.submit(a.bundle)
                ok = result.installed
            else:
                result = ctx.host.call(a.name, json.loads(a.args))
                ok = result.ok
            print(json.dumps(to_jsonable(result), indent=2, ensure_ascii=False))
            status = "ok" if ok else "failed"
        finally:
            ctx.events.emit(EventType.RUN_FINISHED, status=status)
        return 0 if status == "ok" else 1

    from harness.agent.loop import MODELS, run_session

    if missing := [str(f) for f in a.attach if not f.is_file()]:
        p.error(f"--attach: not a file: {', '.join(missing)}")
    ctx.events.emit(
        EventType.RUN_STARTED, task=a.task, pid=os.getpid(), auth=config.AUTH, models=MODELS,
        registry=[e.manifest.ref for e in ctx.registry.list()], attached=[f.name for f in a.attach],
    )  # fmt: skip
    status = "failed"
    try:
        print(run_session(a.task, ctx, attach=a.attach))
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
