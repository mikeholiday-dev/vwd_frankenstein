"""Evidence report, submission package and preflight. Owner: C."""

from __future__ import annotations

import importlib.util
import subprocess
import sys

import pytest

from harness import config
from harness.contracts import EventType
from harness.ops.events import EventLog


def _load(name: str):
    path = config.ROOT / "scripts" / f"{name}.py"
    sys.path.insert(0, str(path.parent))  # the scripts import each other by bare name
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # dataclasses with string annotations look their module up here
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(path.parent))
    return module


@pytest.fixture(scope="module")
def evidence():
    return _load("evidence")


@pytest.fixture(scope="module")
def package_submission():
    return _load("package_submission")


@pytest.fixture(scope="module")
def preflight():
    return _load("preflight")


def _run(path, session, task, ref, *, fake=False, installed=True):
    """One agent run: a gap, a passing test, an approval, an install and a call."""
    log = EventLog(path, session=session)
    extra = {"fake": True} if fake else {}
    log.emit(EventType.RUN_STARTED, task=task, attached=["a.pdf"], registry=[], **extra)
    log.emit(EventType.GAP, gap="look it up", why="no tool", inputs=["id"], outputs=["name"], kind="new", **extra)
    log.emit(EventType.TEST_RUN, ref=ref, passed=True, output="1 passed", **extra)
    log.emit(EventType.APPROVAL_REQUESTED, id="r1", ref=ref, manifest={}, permissions_diff="", test_report="", code="", **extra)
    console = EventLog(path, session="ui")  # the console writes decisions under its own run id
    console.emit(EventType.APPROVAL_DECIDED, request_id="r1", approved=True, by="operator")
    log.emit(EventType.INSTALL, ref=ref, installed=installed, reason="approved", **extra)
    log.emit(EventType.CALL, call_id="c1", ref=ref, ok=True, **extra)
    log.emit(EventType.ANSWER, text="done", call_ids=["c1"], **extra)
    log.emit(EventType.RUN_FINISHED, status="ok", **extra)
    return log.run_id


def test_evidence_files_the_console_decision_under_the_request_run(evidence, tmp_path):
    path = tmp_path / "events.jsonl"
    run_id = _run(path, "A", "task one", "lookup@1")
    runs = evidence.summarize(EventLog(path).read_from(0)[0])
    assert [r.run_id for r in runs] == [run_id]  # the console's own run isn't a run
    assert runs[0].approvals == [("lookup@1", True, "operator")]
    assert runs[0].attached == ["a.pdf"]
    assert runs[0].status == "ok"


def test_evidence_reuse_comes_from_the_log_not_the_agent(evidence, tmp_path):
    path = tmp_path / "events.jsonl"
    _run(path, "A", "first", "lookup@1")
    second = EventLog(path, session="B")
    second.emit(EventType.RUN_STARTED, task="second")
    second.emit(EventType.CALL, call_id="c9", ref="lookup@1", ok=True)
    second.emit(EventType.RUN_FINISHED, status="ok")
    runs = evidence.summarize(EventLog(path).read_from(0)[0])
    assert runs[0].reused_from_earlier == set()
    assert runs[1].reused_from_earlier == {"lookup@1"}


def test_evidence_labels_fake_runs(evidence, tmp_path):
    path = tmp_path / "events.jsonl"
    _run(path, "A", "scripted", "lookup@1", fake=True)
    runs = evidence.summarize(EventLog(path).read_from(0)[0])
    assert runs[0].fake
    assert "fake" in evidence.render(runs).lower()


def test_evidence_lists_operator_actions(evidence, tmp_path):
    log = EventLog(tmp_path / "events.jsonl", session="ui")
    log.emit(EventType.KILL, by="operator")
    log.emit(EventType.ROLLBACK, name="lookup", version=1, by="operator")
    log.emit(EventType.QUARANTINE, name="lookup", by="operator")
    lines = evidence.operator_actions(log.read_from(0)[0])
    assert len(lines) == 3
    assert "kill" in lines[0] and "rolled back" in lines[1] and "quarantined" in lines[2]


def test_package_refuses_a_log_with_fake_events(package_submission, tmp_path):
    path = tmp_path / "events.jsonl"
    _run(path, "A", "scripted", "lookup@1", fake=True)
    with pytest.raises(SystemExit, match="fake"):
        package_submission.package(path, tmp_path / "registry", tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_package_refuses_an_empty_log(package_submission, tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text("")
    with pytest.raises(SystemExit, match="no runs"):
        package_submission.package(path, tmp_path / "registry", tmp_path / "out")


def test_package_bundles_the_registry_and_changes_nothing(package_submission, tmp_path):
    path = tmp_path / "events.jsonl"
    _run(path, "A", "task one", "lookup@1")
    registry = tmp_path / "registry"
    registry.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(registry), *a], check=True, capture_output=True, text=True)
    git("init", "-q")
    (registry / "x.txt").write_text("x")
    git("add", ".")
    git("-c", "user.name=frankenstein-agent", "-c", "user.email=a@b.c", "commit", "-q", "-m", "install lookup@1")
    before_log, before_head = path.read_bytes(), git("rev-parse", "HEAD").stdout

    notes = package_submission.package(path, registry, tmp_path / "out")

    out = tmp_path / "out"
    assert {"events.jsonl", "EVIDENCE.md", "registry.bundle", "registry-head.txt", "registry-git-log.txt"} <= {p.name for p in out.iterdir()}
    assert (out / "registry-head.txt").read_text().strip() == before_head.strip()
    assert any("registry.bundle" in n for n in notes)
    assert path.read_bytes() == before_log and git("rev-parse", "HEAD").stdout == before_head
    clone = subprocess.run(["git", "clone", "-q", str(out / "registry.bundle"), str(tmp_path / "restored")], capture_output=True, text=True)
    assert clone.returncode == 0, clone.stderr


def test_package_says_when_the_registry_is_left_out(package_submission, tmp_path):
    path = tmp_path / "events.jsonl"
    _run(path, "A", "task one", "lookup@1")
    notes = package_submission.package(path, tmp_path / "no-registry", tmp_path / "out")
    assert any("NOT included" in n for n in notes)
    assert not (tmp_path / "out" / "registry.bundle").exists()


def test_preflight_flags_an_api_key_and_fakes(preflight):
    rows = preflight.checks(offline=True, env={"ANTHROPIC_API_KEY": "sk-x", "FRANK_FAKE": "sandbox", "FRANK_MODE": "demo"})
    level = {what.split("=")[0].split(" ")[0]: lvl for lvl, what, _ in rows}
    assert level["FRANK_FAKE"] != "OK"
    assert any(lvl == "FAIL" and "ANTHROPIC_API_KEY" in what for lvl, what, _ in rows)
    assert not any("sk-x" in f"{what} {detail}" for _, what, detail in rows)  # the key itself is never printed


def test_preflight_offline_does_not_touch_the_network(preflight, monkeypatch):
    import socket

    def no_network(*a, **k):
        raise AssertionError("network used in --offline")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    preflight.checks(offline=True, env={"FRANK_MODE": "demo"})
