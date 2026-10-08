"""Operator console API and the scripted runs that drive it. Owner: C."""

import importlib.util
from pathlib import Path

import pytest
from fastapi import HTTPException

from harness import config, fakes
from harness.contracts import REQUIRED_FIELDS, EventType, Manifest
from harness.kernel.limits import LIMITS, MAX_REPAIRS_PER_GAP
from harness.ops.events import EventLog

BUNDLE = Path(__file__).parent / "fixtures" / "bundles" / "echo_ok"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def console(events, tmp_path, monkeypatch):
    from ui import app

    registry = fakes.DirRegistry(tmp_path / "registry")
    monkeypatch.setattr(app, "events", events)
    monkeypatch.setattr(app, "make_registry", lambda: registry)
    return app, registry


@pytest.fixture
def fake_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_PATH", tmp_path / "events.jsonl")
    return _load("fake_run", config.ROOT / "scripts" / "fake_run.py")


def types(log: EventLog) -> list[EventType]:
    return [e.type for e in log.read_from(0)[0]]


def test_decision_and_kill_go_through_the_log(console, events):
    app, _ = console
    app.approve("req-1", app.Decision(approved=False, reason="too many domains"))
    app.kill()
    decided, kill = events.read_from(0)[0]
    assert decided.type == EventType.APPROVAL_DECIDED
    assert decided.data == {"request_id": "req-1", "approved": False, "by": "operator", "reason": "too many domains"}
    assert kill.type == EventType.KILL and kill.data["by"] == "operator"


def test_config_exposes_the_caps(console):
    app, _ = console
    assert app.settings()["limits"] == LIMITS


def test_rollback_and_quarantine_are_logged(console, events):
    app, registry = console
    manifest = Manifest.load(BUNDLE)
    registry.install(BUNDLE, manifest)
    assert app.rollback(manifest.name, app.Version(version=manifest.version))["manifest"]["name"] == manifest.name
    app.quarantine(manifest.name)
    assert types(events) == [EventType.ROLLBACK, EventType.QUARANTINE]
    assert app.registry()[0]["status"] == "quarantined"


def test_registry_log_shows_who_wrote_what(console, tmp_path, monkeypatch):
    from harness.kernel.registry import GitRegistry

    app, _ = console
    monkeypatch.setattr(config, "REGISTRY_DIR", tmp_path / "git-registry")
    assert app.registry_log() == []
    registry = GitRegistry(config.REGISTRY_DIR)
    [created] = app.registry_log()
    manifest = Manifest.load(BUNDLE)
    registry.install(BUNDLE, manifest)
    registry.quarantine(manifest.name)
    quarantine, install, first = app.registry_log()
    assert first["sha"] == created["sha"]
    assert install["author"] == "frankenstein-agent" and manifest.ref in install["refs"]
    assert quarantine["author"] == "frankenstein-operator"


def test_unknown_capability_is_a_404_and_logs_nothing(console, events):
    app, registry = console
    manifest = Manifest.load(BUNDLE)
    registry.install(BUNDLE, manifest)
    for call in (lambda: app.rollback(manifest.name, app.Version(version=99)), lambda: app.rollback("nope", app.Version(version=1)), lambda: app.quarantine("nope")):
        with pytest.raises(HTTPException) as err:
            call()
        assert err.value.status_code == 404
    assert types(events) == []


@pytest.mark.parametrize("scenario,status", [("task1", "ok"), ("upgrade", "ok"), ("capped", "capped")])
def test_fake_scenarios_emit_valid_events(fake_run, scenario, status):
    assert fake_run.play(scenario, speed=1000, auto=True) == status
    log = EventLog(config.LOG_PATH)
    seen = log.read_from(0)[0]
    assert all(e.data.get("fake") or e.type == EventType.APPROVAL_DECIDED for e in seen)
    assert all(k in e.data for e in seen for k in REQUIRED_FIELDS[e.type])
    assert seen[-1].type == EventType.RUN_FINISHED


def test_fake_upgrade_shows_a_permissions_diff(fake_run):
    fake_run.play("upgrade", speed=1000, auto=True)
    req = next(e for e in EventLog(config.LOG_PATH).read_from(0)[0] if e.type == EventType.APPROVAL_REQUESTED)
    assert req.data["previous"]["version"] == 1
    assert req.data["permissions_diff"]["added"] == {"network": ["accounts.example"], "filesystem": ["registry_ro"]}


def test_fake_capped_stops_after_the_repair_cap(fake_run):
    fake_run.play("capped", speed=1000, auto=True)
    seen = EventLog(config.LOG_PATH).read_from(0)[0]
    hit = next(e for e in seen if e.type == EventType.CAP_HIT)
    assert hit.data["limit"] == "repairs_per_gap" and hit.data["value"] == MAX_REPAIRS_PER_GAP + 1
    assert sum(e.type == EventType.BUILD and e.data["role"] == "repair" for e in seen) == MAX_REPAIRS_PER_GAP
    assert EventType.INSTALL not in types(EventLog(config.LOG_PATH))


def test_fake_run_stops_on_kill(fake_run):
    run = fake_run.Run("A", speed=1000, auto=True)
    run.emit(EventType.PLAN, text="one")
    EventLog(config.LOG_PATH, session="ui").emit(EventType.KILL, by="operator")
    with pytest.raises(fake_run.Killed):
        run.emit(EventType.PLAN, text="two")


def test_fresh_start_moves_everything_aside(tmp_path, monkeypatch):
    for name, value in (("LOG_PATH", tmp_path / "logs" / "events.jsonl"), ("REGISTRY_DIR", tmp_path / "registry"), ("WORK_DIR", tmp_path / "work")):
        monkeypatch.setattr(config, name, value)
    fresh = _load("fresh_start", config.ROOT / "scripts" / "fresh_start.py")
    archive = tmp_path / "rehearsals"
    assert fresh.fresh_start(archive=archive) is None

    EventLog(config.LOG_PATH).emit(EventType.KILL, by="operator")
    fakes.DirRegistry(config.REGISTRY_DIR).install(BUNDLE, Manifest.load(BUNDLE))
    dest = fresh.fresh_start("take-1", archive=archive)

    assert dest.name.endswith("-take-1")
    assert (dest / "events.jsonl").read_text().count("\n") == 1
    assert (dest / "registry").is_dir() and not config.REGISTRY_DIR.exists()
    assert config.LOG_PATH.read_text() == ""
