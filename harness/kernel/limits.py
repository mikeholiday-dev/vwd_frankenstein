"""Caps enforced in code (plan §6). Owner: A.

The agent can't touch this file: it lives outside the agent's workspace. Every
counter change emits a `budget` event (the UI meter), every cap hit emits
`cap_hit` and raises. `check()` also honours the UI kill switch.
"""

from __future__ import annotations

import time

from harness.contracts import EventType
from harness.ops.approvals import Killed
from harness.ops.events import EventLog

MAX_GAPS_PER_RUN = 4
MAX_REPAIRS_PER_GAP = 3
MAX_AGENT_TURNS = 60
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
    "turns": MAX_AGENT_TURNS,
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
        self.turns = 0
        self.gaps = 0
        self.repairs: dict[str, int] = {}
        self._kill_offset = events.end()

    @property
    def minutes(self) -> float:
        return (time.monotonic() - self.started) / 60

    def charge(self, model: str, input_tokens: int, output_tokens: int, cache_tokens: int = 0) -> None:
        price_in, price_out = PRICES_PER_MTOK[model]
        self.charge_usd(((input_tokens + cache_tokens) * price_in + output_tokens * price_out) / 1_000_000)

    def charge_usd(self, usd: float) -> None:
        self.usd += usd
        self._changed()

    def turn(self) -> None:
        self.turns += 1
        self._changed()

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
        self._cap("turns", self.turns)
        self._cap("gaps", self.gaps)
        self._cap("minutes", round(self.minutes, 2))

    def snapshot(self) -> dict:
        return {"usd": round(self.usd, 4), "turns": self.turns, "gaps": self.gaps, "minutes": round(self.minutes, 2), "limits": LIMITS}

    def _changed(self) -> None:
        self.events.emit(EventType.BUDGET, **self.snapshot())
        self.check()

    def _cap(self, limit: str, value: float) -> None:
        if value > LIMITS[limit]:
            self.events.emit(EventType.CAP_HIT, limit=limit, value=value, max=LIMITS[limit])
            raise CapExceeded(limit, value, LIMITS[limit])
