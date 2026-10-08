"""Replay a scripted task-1 run into the event log so the UI can be built without the agent. Owner: C.

    uv run python scripts/fake_run.py            # blocks on the approval card in the UI
    uv run python scripts/fake_run.py --auto     # approves itself

Writes events only. It doesn't touch the registry, and every event is
marked `"fake": true`, so never record the video against this.
"""

from __future__ import annotations

import argparse
import time

from harness import config
from harness.contracts import ApprovalRequest, EventType, Kind, TestReport
from harness.kernel.limits import LIMITS
from harness.ops.approvals import LogApprover, decide
from harness.ops.events import EventLog

MANIFEST = {
    "name": "company_lookup", "version": 1, "kind": "code_tool",
    "description": "Look up a company in a business register by its 8-digit id.",
    "interface": {"input": {"ico": "string, 8 digits"}, "output": {"name": "string", "address": "string", "vat_id": "string|null"}},
    "permissions": {"network": ["register.example"], "filesystem": "none", "secrets": []},
    "dependencies": ["httpx"], "tests": {"unit": "tests/test_unit.py"}, "origin": {"task_id": "t-fake", "session": "A"}, "uses": [],
}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--auto", action="store_true")
    p.add_argument("--speed", type=float, default=1.0)
    a = p.parse_args()

    log = EventLog(config.LOG_PATH, session="A")
    usd = 0.0

    def emit(t, pause=0.8, **data):
        time.sleep(pause / a.speed)
        log.emit(t, fake=True, **data)

    def budget(turns, gaps, add):
        nonlocal usd
        usd += add
        emit(EventType.BUDGET, 0.1, usd=round(usd, 3), turns=turns, gaps=gaps, minutes=round(turns * 0.3, 1), limits=LIMITS)

    emit(EventType.RUN_STARTED, 0, task="Is the supplier with IČO 27082440 a reliable VAT payer, and what's their registered address?", registry=[])
    emit(EventType.PLAN, text="1. look up the company by id  2. check VAT payer status  3. answer")
    budget(1, 0, 0.02)
    emit(EventType.GAP, gap="look up a company by its business id", why="no installed capability returns company records",
         inputs={"ico": "string, 8 digits"}, outputs=MANIFEST["interface"]["output"], kind=Kind.CODE_TOOL, registry_search="registry empty")
    budget(2, 1, 0.03)
    emit(EventType.STUDY, query="company register open data API by business id")
    emit(EventType.BUILD, ref="company_lookup@v1", role="builder", attempt=1, files=["capability.py", "manifest.yaml"])
    emit(EventType.BUILD, ref="company_lookup@v1", role="tester", attempt=1, files=["tests/test_unit.py", "tests/fixtures/ok.json"])
    budget(5, 1, 0.41)
    emit(EventType.TEST_RUN, 1.5, ref="company_lookup@v1", passed=False, exit_code=1, duration_s=2.1, sandbox_run_id="fake1",
         output="F..\nFAILED tests/test_unit.py::test_not_found - expected LookupError, got HTTPStatusError(404)\n1 failed, 2 passed")
    emit(EventType.BUILD, ref="company_lookup@v1", role="repair", attempt=2, files=["capability.py"])
    budget(7, 1, 0.22)
    report = TestReport(ref="company_lookup@v1", passed=True, exit_code=0, output="...\n3 passed in 1.9s", duration_s=1.9, sandbox_run_id="fake2")
    emit(EventType.TEST_RUN, 1.5, **vars(report))

    req = ApprovalRequest(id=f"fake-{int(time.time())}", ref="company_lookup@v1", manifest=MANIFEST, previous=None,
                          permissions_diff={"added": {"network": ["register.example"]}, "removed": {}}, test_report=report,
                          code="def run(ico: str) -> dict:\n    ...\n")
    if a.auto:
        log.emit(EventType.APPROVAL_REQUESTED, fake=True, **vars(req))
        decide(log, req.id, True, by="auto-dev")
        approved = True
    else:
        print("waiting for approval in the UI...")
        approved = LogApprover(log).request(req).approved
    emit(EventType.INSTALL, ref="company_lookup@v1", installed=approved, reason="approved by operator" if approved else "rejected")
    if approved:
        emit(EventType.CALL, call_id="call-fake01", ref="company_lookup@v1", ok=True, duration_s=0.4, output={"name": "Example a.s."})
        emit(EventType.ANSWER, text="Example a.s., registered at Example street 1, Prague. (fake run)", call_ids=["call-fake01"])
    emit(EventType.RUN_FINISHED, status="ok" if approved else "failed")


if __name__ == "__main__":
    main()
