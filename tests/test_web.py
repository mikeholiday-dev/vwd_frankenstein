"""Owner: D. Mirrors tests/test_console.py's patterns: the dashboard reads and writes the same
shared event log and registry the console does, so it's tested the same way."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from channels import web
from channels.credentials import CredentialStore
from harness import fakes
from harness.contracts import EventType, Manifest
from harness.kernel.limits import LIMITS
from harness.ops.events import EventLog

BUNDLE = Path(__file__).parent / "fixtures" / "bundles" / "echo_ok"


@pytest.fixture
def dashboard(events, tmp_path, monkeypatch):
    registry = fakes.DirRegistry(tmp_path / "registry")
    store = CredentialStore(tmp_path / "credentials.json")
    monkeypatch.setattr(web, "events", events)
    monkeypatch.setattr(web, "store", store)
    monkeypatch.setattr(web, "make_registry", lambda: registry)
    return web, registry, store


# ---- credentials (unchanged behaviour, now under /api/credentials on the same app) -----------


def test_status_starts_unconfigured(dashboard):
    w, _, _ = dashboard
    status = w.credential_status()
    assert status.keys() == {"telegram", "elevenlabs", "apify"}
    assert all(s == {"configured": False, "remembered": False} for s in status.values())


def test_set_with_remember_marks_it_remembered(dashboard):
    w, _, _ = dashboard
    w.set_credential("telegram", w.SetCredential(value="tok", remember=True))
    assert w.credential_status()["telegram"] == {"configured": True, "remembered": True}


def test_the_value_itself_never_comes_back(dashboard):
    w, _, _ = dashboard
    w.set_credential("apify", w.SetCredential(value="super-secret-token", remember=True))
    for s in w.credential_status().values():
        assert "super-secret-token" not in str(s)


def test_forget_clears_it(dashboard):
    w, _, _ = dashboard
    w.set_credential("telegram", w.SetCredential(value="tok", remember=True))
    w.forget_credential("telegram")
    assert w.credential_status()["telegram"] == {"configured": False, "remembered": False}


def test_unknown_service_is_a_404(dashboard):
    w, _, _ = dashboard
    with pytest.raises(HTTPException) as err:
        w.set_credential("not-a-service", w.SetCredential(value="x"))
    assert err.value.status_code == 404


def test_index_serves_the_dashboard_page(dashboard):
    w, _, _ = dashboard
    assert "Frankenstein dashboard" in w.index()


# ---- config, approvals, kill, registry: parity with ui/app.py --------------------------------


def test_config_exposes_the_caps(dashboard):
    w, _, _ = dashboard
    assert w.api_config()["limits"] == LIMITS


def test_decision_and_kill_go_through_the_log(dashboard, events):
    w, _, _ = dashboard
    w.approve("req-1", w.Decision(approved=False, reason="too many domains"))
    w.kill()
    decided, kill = events.read_from(0)[0]
    assert decided.type == EventType.APPROVAL_DECIDED
    assert decided.data == {"request_id": "req-1", "approved": False, "by": "operator", "reason": "too many domains"}
    assert kill.type == EventType.KILL and kill.data["by"] == "operator"


def test_rollback_and_quarantine_are_logged(dashboard, events):
    w, registry, _ = dashboard
    manifest = Manifest.load(BUNDLE)
    registry.install(BUNDLE, manifest)
    assert w.rollback(manifest.name, w.Version(version=manifest.version))["manifest"]["name"] == manifest.name
    w.quarantine(manifest.name)
    types = [e.type for e in events.read_from(0)[0]]
    assert types == [EventType.ROLLBACK, EventType.QUARANTINE]
    assert w.registry()[0]["status"] == "quarantined"


def test_unknown_capability_is_a_404_and_logs_nothing(dashboard, events):
    w, registry, _ = dashboard
    manifest = Manifest.load(BUNDLE)
    registry.install(BUNDLE, manifest)
    for call in (lambda: w.rollback(manifest.name, w.Version(version=99)), lambda: w.rollback("nope", w.Version(version=1)), lambda: w.quarantine("nope")):
        with pytest.raises(HTTPException) as err:
            call()
        assert err.value.status_code == 404
    assert events.read_from(0)[0] == []


# ---- summary: built on scripts.evidence.summarize, so this is really an integration test ------


def test_summary_is_empty_with_no_runs(dashboard):
    w, _, _ = dashboard
    s = w.summary()
    assert s == {"total_runs": 0, "by_status": {}, "total_usd": 0, "success_rate": None, "runs": []}


def test_summary_counts_a_finished_run(dashboard, events):
    w, _, _ = dashboard
    events.emit(EventType.RUN_STARTED, task="look up a supplier")
    events.emit(EventType.ANSWER, text="the answer", call_ids=[])
    events.emit(EventType.BUDGET, usd=0.42, turns=3, gaps=0, minutes=1.0, limits=LIMITS)
    events.emit(EventType.RUN_FINISHED, status="ok")

    s = w.summary()
    assert s["total_runs"] == 1
    assert s["by_status"] == {"ok": 1}
    assert s["success_rate"] == 100.0
    assert s["total_usd"] == 0.42
    [run] = s["runs"]
    assert run["task"] == "look up a supplier"
    assert run["answer"] == "the answer"
    assert run["usd"] == 0.42


def test_summary_tracks_an_unfinished_run_as_active(dashboard, events):
    w, _, _ = dashboard
    events.emit(EventType.RUN_STARTED, task="still going")
    s = w.summary()
    assert s["by_status"] == {"unfinished": 1}
    assert s["success_rate"] is None  # no finished runs yet to compute a rate from


# ---- new task ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_task_starts_runner_run_task_and_returns_its_session(dashboard, monkeypatch):
    w, _, _ = dashboard
    captured = {}

    async def fake_run_task(task, *, session, models, secrets=None):
        captured.update(task=task, session=session, models=models, secrets=secrets)
        return
        yield  # pragma: no cover (makes this an async generator function)

    monkeypatch.setattr(w.runner, "run_task", fake_run_task)
    result = await w.new_task(w.NewTask(task="look something up", models="full"))

    assert result["session"].startswith("web-")
    [t] = list(w._background_tasks)
    await t  # let the background task actually run before asserting
    assert captured == {"task": "look something up", "session": result["session"], "models": "full", "secrets": {}}


@pytest.mark.asyncio
async def test_new_task_passes_the_stores_offered_secrets(dashboard, monkeypatch):
    w, _, store = dashboard
    store.set("apify", "apify-tok")
    captured = {}

    async def fake_run_task(task, *, session, models, secrets=None):
        captured["secrets"] = secrets
        return
        yield  # pragma: no cover

    monkeypatch.setattr(w.runner, "run_task", fake_run_task)
    await w.new_task(w.NewTask(task="x"))
    [t] = list(w._background_tasks)
    await t
    assert captured["secrets"] == {"APIFY_TOKEN": "apify-tok"}


@pytest.mark.asyncio
async def test_new_task_rejects_an_empty_task(dashboard):
    w, _, _ = dashboard
    with pytest.raises(HTTPException) as err:
        await w.new_task(w.NewTask(task="   "))
    assert err.value.status_code == 400


@pytest.mark.asyncio
async def test_new_task_rejects_an_unknown_model_tier(dashboard):
    w, _, _ = dashboard
    with pytest.raises(HTTPException) as err:
        await w.new_task(w.NewTask(task="x", models="ultra"))
    assert err.value.status_code == 400


@pytest.mark.asyncio
async def test_new_task_defaults_to_the_cheap_tier(dashboard, monkeypatch):
    w, _, _ = dashboard
    captured = {}

    async def fake_run_task(task, *, session, models, secrets=None):
        captured["models"] = models
        return
        yield  # pragma: no cover

    monkeypatch.setattr(w.runner, "run_task", fake_run_task)
    await w.new_task(w.NewTask(task="x"))
    [t] = list(w._background_tasks)
    await t
    assert captured["models"] == "cheap"


def test_summary_mixes_runs_from_every_channel(dashboard, events):
    """A run started under any session (cli, telegram, ...) is counted — the dashboard isn't
    scoped to only what it itself started."""
    w, _, _ = dashboard
    telegram_run = EventLog(events.path, session="telegram-1")
    telegram_run.emit(EventType.RUN_STARTED, task="from telegram")
    telegram_run.emit(EventType.RUN_FINISHED, status="ok")
    cli_run = EventLog(events.path, session="dev")
    cli_run.emit(EventType.RUN_STARTED, task="from the cli")
    cli_run.emit(EventType.RUN_FINISHED, status="failed")

    s = w.summary()
    assert s["total_runs"] == 2
    assert s["by_status"] == {"ok": 1, "failed": 1}
