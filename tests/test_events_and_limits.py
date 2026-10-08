import pytest

from harness.contracts import EventType
from harness.kernel.limits import MAX_AGENT_TURNS, Budget, CapExceeded
from harness.ops.approvals import Killed


def test_emit_validates_required_fields(events):
    with pytest.raises(ValueError, match="call_ids"):
        events.emit(EventType.ANSWER, text="no provenance")


def test_read_from_resumes_and_skips_partial_lines(events):
    a = events.emit(EventType.PLAN, text="one")
    _, offset = events.read_from(0)
    with events.path.open("a") as f:
        f.write('{"partial": ')  # a writer mid-line
    new, offset2 = events.read_from(offset)
    assert new == [] and offset2 == offset
    assert events.read_from(0)[0][0].id == a.id


def test_turn_cap_logs_and_raises(events):
    budget = Budget(events)
    with pytest.raises(CapExceeded):
        for _ in range(MAX_AGENT_TURNS + 1):
            budget.turn()
    assert events.read_from(0)[0][-1].type == EventType.CAP_HIT


def test_usd_is_counted_from_tokens(events):
    budget = Budget(events)
    budget.charge("claude-opus-5-5", 500_000, 50_000)  # $2 in + $1 out
    assert budget.usd == pytest.approx(3.0)
    with pytest.raises(CapExceeded):
        budget.charge("claude-opus-5-5", 500_000, 50_000)


def test_kill_switch_from_another_writer(events):
    budget = Budget(events)
    events.emit(EventType.KILL, by="operator")
    with pytest.raises(Killed):
        budget.check()
