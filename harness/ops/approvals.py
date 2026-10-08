"""Operator approval over the event log. Owner: C.

The agent process emits `approval_requested` and blocks. The UI shows the card
and appends `approval_decided` with the same request id. Approval, kill and
rollback all travel through the log, so the agent and the UI can be separate
processes, and every decision is in the record.
"""

from __future__ import annotations

import time

from harness.contracts import ApprovalDecision, ApprovalRequest, EventType
from harness.ops.events import EventLog


class Killed(Exception):
    pass


class LogApprover:
    def __init__(self, events: EventLog, poll_s: float = 0.5):
        self.events = events
        self.poll_s = poll_s

    def request(self, req: ApprovalRequest) -> ApprovalDecision:
        offset = self.events.emit(EventType.APPROVAL_REQUESTED, **vars(req)).id
        while True:
            new, offset = self.events.read_from(offset)
            for e in new:
                if e.type == EventType.APPROVAL_DECIDED and e.data["request_id"] == req.id:
                    return ApprovalDecision(**{k: e.data[k] for k in ("request_id", "approved", "by")}, reason=e.data.get("reason", ""))
                if e.type == EventType.KILL:
                    raise Killed(e.data["by"])
            time.sleep(self.poll_s)


def decide(events: EventLog, request_id: str, approved: bool, by: str = "operator", reason: str = "") -> None:
    """What the UI calls when the operator clicks approve/reject."""
    events.emit(EventType.APPROVAL_DECIDED, request_id=request_id, approved=approved, by=by, reason=reason)
