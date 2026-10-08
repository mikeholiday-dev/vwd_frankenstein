import pytest

from harness.contracts import EventType
from harness.kernel.limits import MAX_PLANNER_TURNS, MAX_TURNS_PER_GAP, Budget, CapExceeded
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
    with pytest.raises(CapExceeded, match="planner_turns"):
        for _ in range(MAX_PLANNER_TURNS + 1):
            budget.turn()
    assert events.read_from(0)[0][-1].type == EventType.CAP_HIT


def test_each_gap_has_its_own_turn_cap(events):
    budget = Budget(events)
    for gap in ("gap-a", "gap-b"):  # two full builds fit, and neither eats the planner's turns
        with budget.building(gap):
            for _ in range(MAX_TURNS_PER_GAP):
                budget.turn()
    budget.turn()
    assert (budget.planner_turns, budget.turns) == (1, 2 * MAX_TURNS_PER_GAP + 1)

    with budget.building("gap-c"), pytest.raises(CapExceeded, match="turns_per_gap"):
        for _ in range(MAX_TURNS_PER_GAP + 1):
            budget.turn()
    assert events.read_from(0)[0][-1].data["limit"] == "turns_per_gap"
    assert budget.building_gap is None


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


def test_spend_after_a_stop_is_recorded_without_a_second_cap_hit(events):
    budget = Budget(events)
    events.emit(EventType.KILL, by="operator")
    with pytest.raises(Killed):
        budget.check()
    budget.charge_usd(9.0, check=False)  # over MAX_USD_PER_RUN, but the run already stopped
    log = events.read_from(0)[0]
    assert budget.usd == 9.0 and log[-1].type == EventType.BUDGET and log[-1].data["usd"] == 9.0
    assert not any(e.type == EventType.CAP_HIT for e in log)


def test_blocks_of_one_response_count_as_one_turn(events):
    budget = Budget(events)
    assert [budget.turn("msg-1"), budget.turn("msg-1"), budget.turn("msg-1"), budget.turn("msg-2"), budget.turn()] == [True, False, False, True, True]
    assert (budget.turns, budget.planner_turns) == (3, 3)
    assert sum(e.type == EventType.BUDGET for e in events.read_from(0)[0]) == 3
