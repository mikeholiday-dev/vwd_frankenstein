"""Caps enforced in code (plan §6). Owner: A.

The agent can't touch this file: it lives outside the agent's workspace. Every
counter change emits a `budget` event (the UI meter), every cap hit emits
`cap_hit` and raises. `check()` also honours the UI kill switch.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from harness.contracts import EventType
from harness.ops.approvals import Killed
from harness.ops.events import EventLog

MAX_GAPS_PER_RUN = 4
MAX_REPAIRS_PER_GAP = 3
MAX_PLANNER_TURNS = 60
# Builder, tester and repairs of one gap together. Measured on task 2: builder ~20, tester ~13, a repair ~10,
# so MAX_REPAIRS_PER_GAP repairs fit with room for a tester re-check. A per-run total would have to cover
# every build the task needs, so it would either stop task 2 halfway or let one runaway build eat the run.
MAX_TURNS_PER_GAP = 80
MAX_USD_PER_RUN = 5.0
MAX_RUN_MINUTES = 20
MAX_SANDBOX_SECONDS = 60

# "usd" is API-equivalent cost: on a subscription nothing is billed per token,
# but the cap still bounds how much work one run can do. Prefer charge_usd() with
# the Agent SDK's reported cost per query; charge() is the fallback from raw usage.
# USD per million tokens (input, output). Cache reads/writes are charged at the
# input rate, which overestimates: the safe direction for a cap.
PRICES_PER_MTOK = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}

LIMITS = {
    "gaps": MAX_GAPS_PER_RUN,
    "repairs_per_gap": MAX_REPAIRS_PER_GAP,
    "turns": MAX_PLANNER_TURNS + MAX_GAPS_PER_RUN * MAX_TURNS_PER_GAP,  # derived: the most a run can take
    "planner_turns": MAX_PLANNER_TURNS,
    "turns_per_gap": MAX_TURNS_PER_GAP,
    "usd": MAX_USD_PER_RUN,
    "minutes": MAX_RUN_MINUTES,
    "sandbox_seconds": MAX_SANDBOX_SECONDS,
}


class CapExceeded(Exception):
    def __init__(self, limit: str, value: float, max: float):
        super().__init__(f"cap hit: {limit} = {value} > {max}")
        self.limit, self.value, self.max = limit, value, max


class Budget:
    def __init__(self, events: EventLog):
        self.events = events
        self.started = time.monotonic()
        self.usd = 0.0
        self.turns = 0  # the run's total, for the meter
        self.planner_turns = 0
        self.gap_turns: dict[str, int] = {}
        self.building_gap: str | None = None  # turns count against this gap while it's being built
        self.gaps = 0
        self.repairs: dict[str, int] = {}
        self._last_message_id: str | None = None
        self._kill_offset = events.end()

    @property
    def minutes(self) -> float:
        return (time.monotonic() - self.started) / 60

    def charge(self, model: str, input_tokens: int, output_tokens: int, cache_tokens: int = 0) -> None:
        price_in, price_out = PRICES_PER_MTOK[model]
        self.charge_usd(((input_tokens + cache_tokens) * price_in + output_tokens * price_out) / 1_000_000)

    def charge_usd(self, usd: float, check: bool = True) -> None:
        """`check=False` records spend after the run was already stopped, without a second cap_hit."""
        self.usd += usd
        if check:
            self._changed()
        else:
            self.events.emit(EventType.BUDGET, **self.snapshot())

    def turn(self, message_id: str | None = None) -> bool:
        """Count one model turn. The SDK yields a message per content block, all sharing the response's `message_id`:
        repeats of the last id are the same turn and aren't counted again. Returns whether this was a new turn."""
        if message_id is not None and message_id == self._last_message_id:
            return False
        self._last_message_id = message_id
        self.turns += 1
        if self.building_gap:
            self.gap_turns[self.building_gap] = self.gap_turns.get(self.building_gap, 0) + 1
        else:
            self.planner_turns += 1
        self._changed()
        return True

    @contextmanager
    def building(self, gap_id: str) -> Iterator[None]:
        """Model turns inside count against `gap_id`'s MAX_TURNS_PER_GAP, not the planner's."""
        outer, self.building_gap = self.building_gap, gap_id
        try:
            yield
        finally:
            self.building_gap = outer

    def gap(self) -> None:
        self.gaps += 1
        self._changed()

    def repair(self, gap_id: str) -> None:
        self.repairs[gap_id] = self.repairs.get(gap_id, 0) + 1
        self._changed()
        self._cap("repairs_per_gap", self.repairs[gap_id])

    def check(self) -> None:
        """Call between agent steps. Raises CapExceeded or Killed."""
        new, self._kill_offset = self.events.read_from(self._kill_offset)
        for e in new:
            if e.type == EventType.KILL:
                raise Killed(e.data["by"])
        self._cap("usd", round(self.usd, 4))
        self._cap("planner_turns", self.planner_turns)
        if self.building_gap:
            self._cap("turns_per_gap", self.gap_turns.get(self.building_gap, 0))
        self._cap("gaps", self.gaps)
        self._cap("minutes", round(self.minutes, 2))

    def snapshot(self) -> dict:
        return {
            "usd": round(self.usd, 4), "turns": self.turns, "gaps": self.gaps, "minutes": round(self.minutes, 2), "limits": LIMITS,
            "planner_turns": self.planner_turns, "gap_turns": self.gap_turns.get(self.building_gap, 0) if self.building_gap else 0,
        }  # fmt: skip

    def _changed(self) -> None:
        self.events.emit(EventType.BUDGET, **self.snapshot())
        self.check()

    def _cap(self, limit: str, value: float) -> None:
        if value > LIMITS[limit]:
            self.events.emit(EventType.CAP_HIT, limit=limit, value=value, max=LIMITS[limit])
            raise CapExceeded(limit, value, LIMITS[limit])
