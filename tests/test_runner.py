"""Owner: D.

No real `frank run` here (that needs Docker and a Claude login): these exercise the
tailing/formatting logic against a hand-built event log, and against a throwaway
subprocess that writes a frank-run-shaped sequence of events itself, so the
subprocess/offset/run_id-matching plumbing is tested for real.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap

import pytest

real_subprocess_exec = asyncio.create_subprocess_exec

from channels.runner import _format, decide_approval, run_task
from harness.contracts import Event, EventType
from harness.ops.events import EventLog


def ev(type: EventType, run_id: str = "r1", session: str = "s", **data) -> Event:
    return Event(id=0, ts="2026-01-01T00:00:00Z", run_id=run_id, session=session, type=type, data=data)


class TestFormat:
    def test_gap_names_the_gap(self):
        upd = _format(ev(EventType.GAP, gap="look up a thing", why="no tool", inputs={}, outputs={}, kind="code_tool"))
        assert "look up a thing" in upd.text

    def test_first_builder_attempt_is_reported(self):
        upd = _format(ev(EventType.BUILD, ref="x@v1", role="builder", attempt=1))
        assert upd is not None and "x@v1" in upd.text

    def test_tester_start_is_quiet(self):
        assert _format(ev(EventType.BUILD, ref="x@v1", role="tester", attempt=1)) is None

    def test_repair_attempt_is_reported(self):
        upd = _format(ev(EventType.BUILD, ref="x@v1", role="repair", attempt=2))
        assert "attempt 2" in upd.text

    def test_test_run_reports_pass_and_fail(self):
        assert "passed" in _format(ev(EventType.TEST_RUN, ref="x@v1", passed=True, output="")).text
        assert "failed" in _format(ev(EventType.TEST_RUN, ref="x@v1", passed=False, output="")).text

    def test_approval_requested_carries_the_raw_request(self):
        data = {"id": "req1", "ref": "x@v1", "manifest": {}, "previous": None, "permissions_diff": {}, "test_report": {}, "code": ""}
        upd = _format(ev(EventType.APPROVAL_REQUESTED, **data))
        assert upd.kind == "approval_requested"
        assert upd.approval == data

    def test_install_reports_the_refusal_reason(self):
        upd = _format(ev(EventType.INSTALL, ref="x@v1", installed=False, reason="refused: missing capability.py"))
        assert "refused" in upd.text

    def test_cap_hit_names_the_limit(self):
        upd = _format(ev(EventType.CAP_HIT, limit="planner_turns", value=61, max=60))
        assert "planner_turns" in upd.text

    def test_unhandled_event_type_is_dropped(self):
        assert _format(ev(EventType.BUDGET, usd=0, turns=0, gaps=0, minutes=0, limits={})) is None


SCRIPT = textwrap.dedent('''
    import sys
    from pathlib import Path
    sys.path.insert(0, {root!r})
    from harness.ops.events import EventLog
    from harness.contracts import EventType

    log = EventLog(Path({log_path!r}), session={session!r})
    log.emit(EventType.RUN_STARTED, task="t")
    log.emit(EventType.GAP, gap="g", why="w", inputs={{}}, outputs={{}}, kind="code_tool")
    log.emit(EventType.INSTALL, ref="x@v1", installed=True, reason="approved")
    log.emit(EventType.ANSWER, text="the answer", call_ids=[])
    log.emit(EventType.RUN_FINISHED, status="ok")
''')


@pytest.mark.asyncio
async def test_run_task_tails_only_its_own_session_and_run_id(tmp_path, monkeypatch):
    from harness import config

    log_path = tmp_path / "events.jsonl"
    other = EventLog(log_path, session="someone-else")
    other.emit(EventType.RUN_STARTED, task="unrelated")
    other.emit(EventType.GAP, gap="not mine", why="w", inputs={}, outputs={}, kind="code_tool")

    script = SCRIPT.format(root=str(config.ROOT), log_path=str(log_path), session="mine")

    async def fake_subprocess_exec(*argv, **kwargs):
        return await real_subprocess_exec(sys.executable, "-c", script, **kwargs)

    monkeypatch.setattr("channels.runner.asyncio.create_subprocess_exec", fake_subprocess_exec)

    updates = [u async for u in run_task("t", session="mine", log_path=log_path)]
    kinds = [u.kind for u in updates]
    assert kinds == ["started", "gap", "install", "answer", "finished"]
    assert "not mine" not in " ".join(u.text for u in updates)
    assert any(u.text == "the answer" for u in updates)


@pytest.mark.asyncio
async def test_run_task_passes_offered_secrets_to_the_subprocess_env(tmp_path, monkeypatch):
    """Not through the bot's own ElevenLabs calls (channels/voice.py) — this is the operator's
    key reaching Frankenstein's own vault (harness/kernel/vault.py), the same way FRANK_SECRETS
    plus a .env file would for a terminal operator."""
    log_path = tmp_path / "events.jsonl"
    captured = {}

    async def fake_subprocess_exec(*argv, **kwargs):
        captured["env"] = kwargs["env"]
        return await real_subprocess_exec(sys.executable, "-c", "import sys; sys.exit(0)", **kwargs)

    monkeypatch.setattr("channels.runner.asyncio.create_subprocess_exec", fake_subprocess_exec)

    async for _ in run_task("t", session="mine", log_path=log_path, secrets={"APIFY_TOKEN": "tok", "ELEVENLABS_API_KEY": "key"}):
        pass

    assert captured["env"]["APIFY_TOKEN"] == "tok"
    assert captured["env"]["ELEVENLABS_API_KEY"] == "key"
    assert set(captured["env"]["FRANK_SECRETS"].split(",")) == {"APIFY_TOKEN", "ELEVENLABS_API_KEY"}


@pytest.mark.asyncio
async def test_run_task_sets_no_frank_secrets_without_offered_secrets(tmp_path, monkeypatch):
    log_path = tmp_path / "events.jsonl"
    captured = {}

    async def fake_subprocess_exec(*argv, **kwargs):
        captured["env"] = kwargs["env"]
        return await real_subprocess_exec(sys.executable, "-c", "import sys; sys.exit(0)", **kwargs)

    monkeypatch.setattr("channels.runner.asyncio.create_subprocess_exec", fake_subprocess_exec)
    async for _ in run_task("t", session="mine", log_path=log_path):
        pass

    assert "FRANK_SECRETS" not in captured["env"]
    assert "APIFY_TOKEN" not in captured["env"]


@pytest.mark.asyncio
async def test_run_task_reports_a_subprocess_that_never_starts(tmp_path, monkeypatch):
    log_path = tmp_path / "events.jsonl"

    async def fake_subprocess_exec(*argv, **kwargs):
        return await real_subprocess_exec(sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(1)", **kwargs)

    monkeypatch.setattr("channels.runner.asyncio.create_subprocess_exec", fake_subprocess_exec)

    updates = [u async for u in run_task("t", session="mine", log_path=log_path)]
    assert updates[-1].kind == "crashed"
    assert "boom" in updates[-1].text


def test_decide_approval_writes_the_same_event_the_console_writes(tmp_path):
    log_path = tmp_path / "events.jsonl"
    decide_approval("req1", True, "telegram:alice", reason="looks fine", log_path=log_path)
    events, _ = EventLog(log_path, session="reader").read_from(0)
    assert len(events) == 1
    e = events[0]
    assert e.type == EventType.APPROVAL_DECIDED
    assert e.data == {"request_id": "req1", "approved": True, "by": "telegram:alice", "reason": "looks fine"}
