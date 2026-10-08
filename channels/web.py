"""The web dashboard. Owner: D.

A separate surface from the operator console (ui/), not a replacement for it: both
read and write the same shared event log and registry (harness/ops/events.py,
harness/wiring.py), so a run or a decision made on one shows up on the other.
This one adds the credentials page and leans toward "configure it, see the
outputs" over the console's deeper build-by-build lab view. `store` and `events`
are module-level singletons so the bot process can run this alongside the
Telegram bot and share state; tests monkeypatch them the same way
tests/test_console.py does for ui/app.py.

  uv run uvicorn channels.web:app --port 8001   # standalone, for UI work on this page alone
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from channels import runner
from channels.credentials import SERVICES, CredentialStore, offered_secrets
from harness import config
from harness.contracts import EventType, to_jsonable
from harness.kernel.limits import LIMITS
from harness.ops.approvals import decide
from harness.ops.events import EventLog
from harness.wiring import make_registry
from scripts.evidence import summarize
from scripts.fresh_start import ARCHIVE, fresh_start

app = FastAPI(title="Frankenstein dashboard")
store = CredentialStore()
events = EventLog(config.LOG_PATH, session="web")
STATIC = Path(__file__).parent / "static"
_background_tasks: set[asyncio.Task] = set()  # keeps a reference so asyncio doesn't GC a running task


class NewTask(BaseModel):
    task: str
    models: str = "cheap"


class SetCredential(BaseModel):
    value: str
    remember: bool = False


class Decision(BaseModel):
    approved: bool
    reason: str = ""


class Version(BaseModel):
    version: int


class Reset(BaseModel):
    force: bool = False  # archive even with unfinished runs in the log (a run whose process died never finishes)


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "dashboard.html").read_text()


@app.get("/dashboard.css")
def css():
    return FileResponse(STATIC / "dashboard.css", media_type="text/css", headers={"Cache-Control": "no-store"})


@app.get("/dashboard.js")
def js():
    return FileResponse(STATIC / "dashboard.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})


# ---- config, events, outputs ---------------------------------------------------------------


@app.get("/api/config")
def api_config():
    return {"limits": LIMITS, "mode": config.MODE, "fakes": sorted(config.FAKES), "approver": config.APPROVER, "auth": config.AUTH, "models": config.MODELS}


@app.get("/api/events")
async def stream(request: Request, offset: int = 0):
    """SSE: every event from `offset` (0 = replay the whole log), then live. Same shape ui/app.py
    serves, so a client could point either console's event feed at either server."""
    async def gen():
        pos = offset
        while not await request.is_disconnected():
            if (events.path.stat().st_size if events.path.exists() else 0) < pos:
                yield "event: reset\ndata: {}\n\n"
                return
            new, pos = events.read_from(pos)
            for e in new:
                yield f"id: {e.id}\nevent: {e.type}\ndata: {json.dumps(to_jsonable(e), ensure_ascii=False)}\n\n"
            if not new:
                await asyncio.sleep(0.25)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/summary")
def summary():
    """Aggregate stats for the overview tiles and charts, and the per-run outputs list — built
    from scripts.evidence.summarize() so "what happened" is computed the same way the submission's
    evidence report computes it, not reimplemented a second time."""
    runs = summarize(events.read_from(0)[0])
    by_status: dict[str, int] = {}
    total_usd = 0.0
    for r in runs:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        total_usd += (r.budget or {}).get("usd", 0)
    done = [r for r in runs if r.status != "unfinished"]
    ok = sum(r.status == "ok" for r in done)
    return {
        "total_runs": len(runs),
        "by_status": by_status,
        "total_usd": round(total_usd, 4),
        "success_rate": round(100 * ok / len(done), 1) if done else None,
        "runs": [
            {
                "run_id": r.run_id, "session": r.session, "task": r.task, "status": r.status, "fake": r.fake,
                "started": r.started, "usd": (r.budget or {}).get("usd", 0),
                "answer": r.answer.get("text") if r.answer else None,
                "built": sorted(r.installed_refs), "reused": sorted(r.reused_from_earlier),
            }
            for r in reversed(runs)
        ],
    }


@app.post("/api/tasks")
async def new_task(body: NewTask):
    """Start a task the same way the Telegram bot or `frank run` at a terminal would — one
    subprocess, through channels.runner.run_task. Its progress isn't streamed back here: the
    dashboard's existing /api/events feed and /api/summary polling already pick it up from
    the shared event log, the same way they show a run started from anywhere else."""
    task = body.task.strip()
    if not task:
        raise HTTPException(400, "task is empty")
    if body.models not in ("cheap", "full"):
        raise HTTPException(400, "models must be 'cheap' or 'full'")
    session = f"web-{uuid.uuid4().hex[:8]}"
    t = asyncio.create_task(_drain(session, task, body.models))
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)
    return {"session": session}


async def _drain(session: str, task: str, models: str) -> None:
    async for _ in runner.run_task(task, session=session, models=models, secrets=offered_secrets(store)):
        pass


# ---- approvals, kill ------------------------------------------------------------------------


@app.post("/api/approvals/{request_id}")
def approve(request_id: str, d: Decision):
    decide(events, request_id, d.approved, by="operator", reason=d.reason)
    return {"ok": True}


@app.post("/api/kill")
def kill():
    events.emit(EventType.KILL, by="operator")
    return {"ok": True}


# ---- registry --------------------------------------------------------------------------------


@app.get("/api/registry")
def registry():
    return [to_jsonable(e) for e in make_registry().list(include_quarantined=True)]


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
        make_registry().quarantine(name)
    except KeyError:
        raise HTTPException(404, f"{name} is not installed") from None
    events.emit(EventType.QUARANTINE, name=name, by="operator")
    return {"ok": True}


@app.post("/api/reset")
def reset(body: Reset):
    """Dev only: start from scratch. Moves the registry, the event log and the build workspaces
    into rehearsals/<timestamp>-dashboard/ through scripts/fresh_start.py; never deletes anything.
    The next make_registry() creates an empty registry, and every open page reloads on the SSE reset."""
    if config.MODE != "dev":
        raise HTTPException(403, "start from scratch is dev mode only")
    unfinished = [r.run_id for r in summarize(events.read_from(0)[0]) if r.status == "unfinished"]
    if unfinished and not body.force:
        raise HTTPException(409, f"{len(unfinished)} run(s) unfinished: {', '.join(unfinished)}. Kill them first, or force if they died")
    try:
        dest = fresh_start(label="dashboard", archive=ARCHIVE)
    except SystemExit as e:  # fresh_start refuses to overwrite an archive; don't let that take the server down
        raise HTTPException(409, str(e)) from None
    return {"archived_to": str(dest) if dest else None}


# ---- credentials ------------------------------------------------------------------------------


@app.get("/api/credentials")
def credential_status():
    """Per service: whether something is currently usable, and whether it's remembered
    across restarts. Never the value itself."""
    remembered = store.remembered()
    return {s: {"configured": store.get(s) is not None, "remembered": s in remembered} for s in SERVICES}


@app.post("/api/credentials/{service}")
def set_credential(service: str, body: SetCredential):
    _check(service)
    store.set(service, body.value, remember=body.remember)
    return {"ok": True}


@app.delete("/api/credentials/{service}")
def forget_credential(service: str):
    _check(service)
    store.forget(service)
    return {"ok": True}


def _check(service: str) -> None:
    if service not in SERVICES:
        raise HTTPException(404, f"unknown service {service!r}")
