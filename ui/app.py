"""Operator console (plan §6). Owner: C.

Reads the shared event log, writes operator decisions back into it. It never
imports the agent: run sessions in their own terminals (`frank run ...`) with
FRANK_APPROVER=ui and they block on the approval cards here.

  uv run uvicorn ui.app:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from harness import config
from harness.contracts import EventType, to_jsonable
from harness.kernel.limits import LIMITS
from harness.ops.approvals import decide
from harness.ops.events import EventLog
from harness.wiring import make_registry

app = FastAPI(title="Frankenstein console")
events = EventLog(config.LOG_PATH, session="ui")
STATIC = Path(__file__).parent / "static"


class Decision(BaseModel):
    approved: bool
    reason: str = ""


class Version(BaseModel):
    version: int


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/config")
def settings():
    """What the console shows before any run: the caps and which parts are fakes."""
    return {"limits": LIMITS, "mode": config.MODE, "fakes": sorted(config.FAKES), "approver": config.APPROVER, "auth": config.AUTH, "models": config.MODELS}


@app.get("/api/events")
async def stream(request: Request, offset: int = 0):
    """SSE: every event from `offset` (0 = replay the whole log), then live."""

    async def gen():
        pos = offset
        while not await request.is_disconnected():
            if (events.path.stat().st_size if events.path.exists() else 0) < pos:  # moved aside by scripts/fresh_start.py
                yield "event: reset\ndata: {}\n\n"
                return
            new, pos = events.read_from(pos)
            for e in new:
                yield f"id: {e.id}\nevent: {e.type}\ndata: {json.dumps(to_jsonable(e), ensure_ascii=False)}\n\n"
            if not new:
                await asyncio.sleep(0.25)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/approvals/{request_id}")
def approve(request_id: str, d: Decision):
    decide(events, request_id, d.approved, by="operator", reason=d.reason)
    return {"ok": True}


@app.post("/api/kill")
def kill():
    events.emit(EventType.KILL, by="operator")
    return {"ok": True}


@app.get("/api/registry")
def registry():
    return [to_jsonable(e) for e in make_registry().list(include_quarantined=True)]


@app.get("/api/registry/log")
def registry_log(limit: int = 50):
    """The registry's git history: every install authored by the agent, every rollback/quarantine by the operator."""
    if not (config.REGISTRY_DIR / ".git").exists():
        return []  # the fake registry has no history
    fmt = "%h%x1f%an%x1f%aI%x1f%s%x1f%D"
    out = subprocess.run(["git", "-C", str(config.REGISTRY_DIR), "log", f"-n{max(1, min(limit, 500))}", f"--format={fmt}"],
                         capture_output=True, text=True)
    if out.returncode:
        return []  # no commits yet
    return [dict(zip(("sha", "author", "date", "subject", "refs"), line.split("\x1f"))) for line in out.stdout.splitlines()]


@app.post("/api/registry/{name}/rollback")
def rollback(name: str, v: Version):
    try:
        entry = make_registry().rollback(name, v.version)
    except KeyError:
        raise HTTPException(404, f"{name}@v{v.version} was never installed") from None
    events.emit(EventType.ROLLBACK, name=name, version=v.version, by="operator")
    return to_jsonable(entry)


@app.post("/api/registry/{name}/quarantine")
def quarantine(name: str):
    try:
        make_registry().quarantine(name)  # TODO(A): also revoke the capability's proxy domains
    except KeyError:
        raise HTTPException(404, f"{name} is not installed") from None
    events.emit(EventType.QUARANTINE, name=name, by="operator")
    return {"ok": True}
