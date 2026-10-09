"""The web dashboard: the operator's one page (plan §6). Owner: D.

Reads the shared event log, writes operator decisions back into it, and never imports the
agent. Start a task, approve or reject installs from the full card (manifest, permissions
diff, the harness's own test log, code), watch the lab and budget of a run, roll back or
quarantine a capability, kill one run or all of them, enter channel credentials. A run
started anywhere else (`frank run` with FRANK_APPROVER=ui, a chat bot) shows up here too and
blocks on the approval card. `store` and `events` are module-level singletons so the bot
process can run this alongside the bots and share state; tests monkeypatch them.

  uv run uvicorn channels.web:app --reload --port 8001   # standalone
  uv run python -m channels.run                          # with the Telegram and Discord bots
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
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
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # generous for an invoice PDF; a hard stop against a pathological upload


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
    return HTMLResponse((STATIC / "dashboard.html").read_text(), headers={"Cache-Control": "no-store"})


@app.get("/dashboard.css")
def css():
    return FileResponse(STATIC / "dashboard.css", media_type="text/css", headers={"Cache-Control": "no-store"})


@app.get("/dashboard.js")
def js():
    return FileResponse(STATIC / "dashboard.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})


# ---- config, events, outputs ---------------------------------------------------------------


@app.get("/api/config")
def api_config():
    """What the page shows before any run: the caps and which parts are fakes."""
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
async def new_task(task: str = Form(...), models: str = Form("cheap"), files: list[UploadFile] = File(default=[])):
    """Start a task the same way the Telegram bot or `frank run` at a terminal would — one
    subprocess, through channels.runner.run_task. Its progress isn't streamed back here: the
    dashboard's existing /api/events feed and /api/summary polling already pick it up from
    the shared event log, the same way they show a run started from anywhere else.

    Multipart, not JSON, so the same request can carry attached files — channels.runner.run_task
    already takes `attach`, the same list of paths --attach FILE builds for the CLI; this just
    gives the dashboard a way to put a file there too, like task 2's invoice PDF."""
    task = task.strip()
    if not task:
        raise HTTPException(400, "task is empty")
    if models not in ("cheap", "full"):
        raise HTTPException(400, "models must be 'cheap' or 'full'")
    if config.MODE == "demo" and models == "cheap":  # the subprocess would refuse it at import and never start
        raise HTTPException(400, "demo mode runs only on full models")
    session = f"web-{uuid.uuid4().hex[:8]}"
    attach = await _save_uploads(session, files)
    t = asyncio.create_task(_drain(session, task, models, attach))
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)
    return {"session": session}


async def _save_uploads(session: str, files: list[UploadFile]) -> list[Path]:
    """Writes each upload under work/uploads/<session>/<name> — the subprocess's own workdir
    tree, so it's cleaned up the same way other per-run build state is. Filenames are taken by
    basename only (Path(...).name strips any directory components a browser might send), so an
    upload can never write outside that folder."""
    named = [f for f in files if f.filename]
    if not named:
        return []
    upload_dir = config.WORK_DIR / "uploads" / session
    upload_dir.mkdir(parents=True, exist_ok=True)
    attach = []
    for f in named:
        data = await f.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(400, f"{f.filename}: larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
        dest = upload_dir / Path(f.filename).name
        dest.write_bytes(data)
        attach.append(dest)
    return attach


async def _drain(session: str, task: str, models: str, attach: list[Path]) -> None:
    async for _ in runner.run_task(task, session=session, models=models, attach=attach, secrets=offered_secrets(store)):
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


KILL_POLL_S = 0.25
KILL_GRACE_POLLS = 20  # ~5s at the default poll interval before escalating to SIGKILL


@app.post("/api/runs/{run_id}/kill")
async def kill_run(run_id: str):
    """Kill just this run, not every run in progress — the harness's own kill event has no
    run scoping (every process's Budget.check() polls the same log, so EventType.KILL always
    stops all of them; that's the global Kill switch). This works a different way: every
    run_started event already records its process's pid, so send that process a real signal
    directly, then write the RUN_FINISHED a self-reported kill would have written, once the
    process is confirmed gone — the harness itself is never told, it just stops running."""
    all_events = events.read_from(0)[0]
    started = next((e for e in all_events if e.run_id == run_id and e.type == EventType.RUN_STARTED), None)
    if started is None:
        raise HTTPException(404, f"no such run: {run_id}")
    if any(e.run_id == run_id and e.type == EventType.RUN_FINISHED for e in all_events):
        raise HTTPException(409, f"run {run_id} already finished")
    pid = started.data.get("pid")
    if not pid:
        raise HTTPException(409, "this run has no recorded pid (an older log, or started a different way)")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        raise HTTPException(409, "that process is already gone") from None
    t = asyncio.create_task(_confirm_kill(run_id, pid))
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)
    return {"ok": True}


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else — treat as alive, nothing more we can do
        return True
    return True


async def _confirm_kill(run_id: str, pid: int) -> None:
    for _ in range(KILL_GRACE_POLLS):
        await asyncio.sleep(KILL_POLL_S)
        if not _process_alive(pid):
            break
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await asyncio.sleep(KILL_POLL_S)
    EventLog(events.path, session="web", run_id=run_id).emit(EventType.RUN_FINISHED, status="killed")


# ---- registry --------------------------------------------------------------------------------


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
                         capture_output=True, text=True, check=False)
    if out.returncode:
        return []  # no commits yet
    return [dict(zip(("sha", "author", "date", "subject", "refs"), line.split("\x1f"))) for line in out.stdout.splitlines()]


@app.post("/api/registry/{name}/rollback")
def rollback(name: str, v: Version):
    reg = make_registry()
    try:
        if reg.get(name).manifest.version == v.version:
            raise HTTPException(409, f"{name}@v{v.version} is already the active version")
        entry = reg.rollback(name, v.version)
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
