"""Spawns one `frank run` per task and turns the shared event log into chat-sized updates. Owner: D.

Each call is its own subprocess, exactly like an operator typing
`frank run --session <s> "<task>"` at a terminal: nothing here imports harness.agent
directly, so it can't drift from what a human running the CLI gets, and a crash in
one chat's run can't take another chat's run down with it. Installs run with
FRANK_APPROVER=ui, so a request can be approved from this bot or the web console,
whichever answers first — both just call harness.ops.approvals.decide.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness import config
from harness.contracts import EventType
from harness.ops.approvals import decide
from harness.ops.events import EventLog


@dataclass
class Progress:
    """One line of what happened, or the final answer.

    `kind` matches the event type that produced it, plus "started", "crashed" and
    "finished". `approval` carries the raw ApprovalRequest dict, set only for kind
    == "approval_requested", so the caller can offer Approve/Reject.
    """

    kind: str
    text: str
    approval: dict[str, Any] | None = None


async def run_task(task: str, *, session: str, attach: list[Path] = (), models: str = "cheap",
                    secrets: dict[str, str] | None = None, log_path: Path | None = None) -> AsyncIterator[Progress]:
    """Run one task and yield Progress updates as the harness's event log reports them.

    `secrets`: e.g. {"APIFY_TOKEN": "...", "ELEVENLABS_API_KEY": "..."} from the operator's own
    credentials (channels/credentials.py), passed through as env vars plus FRANK_SECRETS so this
    run can use the kernel's vault (gateway mode) the same way a terminal operator with a .env
    file would — this is a different use than the bot's own direct ElevenLabs calls (channels/voice.py):
    this lets Frankenstein itself build and call a keyed-API capability, through the egress proxy.
    """
    log_path = log_path or config.LOG_PATH
    events = EventLog(log_path, session=f"{session}-reader")
    offset = events.end()

    argv = [sys.executable, "-m", "harness.cli", "run", "--session", session]
    for f in attach:
        argv += ["--attach", str(f)]
    argv.append(task)
    secrets = {k: v for k, v in (secrets or {}).items() if v}
    env = {**os.environ, "FRANK_APPROVER": "ui", "FRANK_MODELS": models, **secrets}
    if secrets:
        env["FRANK_SECRETS"] = ",".join(secrets)

    proc = await asyncio.create_subprocess_exec(*argv, cwd=str(config.ROOT), env=env,
                                                 stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    yield Progress("started", f"Starting: {task}")

    run_id: str | None = None
    while True:
        new, offset = events.read_from(offset)
        for e in new:
            if run_id is None:
                if e.type == EventType.RUN_STARTED and e.session == session:
                    run_id = e.run_id
                continue
            if e.run_id != run_id:
                continue
            upd = _format(e)
            if upd:
                yield upd
            if e.type == EventType.RUN_FINISHED:
                await proc.wait()
                return
        if proc.returncode is not None and run_id is None:
            stderr = (await proc.stderr.read()).decode(errors="replace").strip()[-800:]
            yield Progress("crashed", f"The run never started.{' ' + stderr if stderr else ''}")
            return
        await asyncio.sleep(0.3)


def decide_approval(request_id: str, approved: bool, by: str, *, reason: str = "", log_path: Path | None = None) -> None:
    """What a Telegram Approve/Reject tap calls — the same call the web console makes."""
    decide(EventLog(log_path or config.LOG_PATH, session="telegram"), request_id, approved, by=by, reason=reason)


def _format(e) -> Progress | None:
    d = e.data
    match e.type:
        case EventType.PLAN:
            return Progress("plan", f"Plan: {d['text']}")
        case EventType.GAP:
            return Progress("gap", f"No installed tool for this — building: {d['gap']}")
        case EventType.BUILD:
            if d["role"] == "repair":
                return Progress("build", f"Tests failed, repairing {d['ref']} (attempt {d['attempt']})")
            if d["role"] == "builder" and d["attempt"] == 1:
                return Progress("build", f"Writing {d['ref']}")
            return None  # the tester starting isn't worth its own message
        case EventType.TEST_RUN:
            mark = "passed" if d["passed"] else "failed"
            return Progress("test_run", f"Tests {mark} for {d['ref']} ({d.get('suite', 'own')})")
        case EventType.APPROVAL_REQUESTED:
            return Progress("approval_requested", f"Needs your approval: {d['ref']}", approval=d)
        case EventType.INSTALL:
            if d["installed"]:
                return Progress("install", f"Installed {d['ref']}")
            return Progress("install", f"Not installed, {d['ref']}: {d['reason']}")
        case EventType.CAP_HIT:
            return Progress("cap_hit", f"Hit a limit: {d['limit']} = {d['value']} (max {d['max']})")
        case EventType.ERROR:
            return Progress("error", d["message"])
        case EventType.ANSWER:
            return Progress("answer", d["text"])
        case EventType.RUN_FINISHED:
            return Progress("finished", d["status"])
        case _:
            return None
