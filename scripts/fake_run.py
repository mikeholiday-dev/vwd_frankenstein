"""Replay scripted runs into the event log so the UI can be built without the agent. Owner: C.

    uv run python scripts/fake_run.py                      # task 1; blocks on the approval card in the UI
    uv run python scripts/fake_run.py --scenario upgrade   # session B: v2 with a permissions diff
    uv run python scripts/fake_run.py --scenario capped    # repairs keep failing until the cap stops the run
    uv run python scripts/fake_run.py --scenario all --auto --speed 5

Scenarios: task1 (default), upgrade, capped, all. Every scenario stops when the
operator hits the kill switch and then logs `run_finished: killed`.

Writes events only. It doesn't touch the registry, and every event is
marked `"fake": true`, so never record the video against this.
"""

from __future__ import annotations

import argparse
import time
import uuid

from harness import config
from harness.contracts import ApprovalRequest, EventType, Kind, Permissions, TestReport, permissions_diff
from harness.kernel.limits import LIMITS, MAX_REPAIRS_PER_GAP
from harness.ops.approvals import Killed, LogApprover, decide
from harness.ops.events import EventLog

TASK_1 = "Is the supplier with IČO 27082440 a reliable VAT payer, and what's their registered address?"
TASK_2 = "Here's an invoice PDF. Check the supplier, verify the bank account is a published one, and give me the total in EUR."

MANIFEST = {
    "name": "company_lookup", "version": 1, "kind": "code_tool",
    "description": "Look up a company in a business register by its 8-digit id.",
    "interface": {"input": {"ico": "string, 8 digits"}, "output": {"name": "string", "address": "string", "vat_id": "string|null"}},
    "permissions": {"network": ["register.example"], "filesystem": "none", "secrets": []},
    "dependencies": ["httpx"], "tests": {"unit": "tests/test_unit.py"}, "origin": {"task_id": "t-fake", "session": "A"}, "uses": [],
}
MANIFEST_V2 = {
    **MANIFEST, "version": 2,
    "description": "Look up a company by its 8-digit id, including its published bank accounts.",
    "interface": {"input": {"ico": "string, 8 digits"},
                  "output": {"name": "string", "address": "string", "vat_id": "string|null", "bank_accounts": "list of strings"}},
    "permissions": {"network": ["register.example", "accounts.example"], "filesystem": "registry_ro", "secrets": []},
    "origin": {"task_id": "t-fake-2", "session": "B"},
}

CODE_V1_BROKEN = '''import httpx

BASE = "https://register.example/companies"


def run(ico: str) -> dict:
    r = httpx.get(f"{BASE}/{ico}", timeout=10)
    r.raise_for_status()
    d = r.json()
    return {"name": d["name"], "address": d["address"], "vat_id": d.get("vat_id")}
'''
CODE_V1 = CODE_V1_BROKEN.replace(
    "    r.raise_for_status()\n",
    '    if r.status_code == 404:\n        raise LookupError(f"no company with id {ico}")\n    r.raise_for_status()\n',
)
CODE_V2 = CODE_V1.replace(
    '    return {"name": d["name"], "address": d["address"], "vat_id": d.get("vat_id")}\n',
    '    accounts = httpx.get(f"https://accounts.example/{ico}", timeout=10).json()["accounts"]\n'
    '    return {"name": d["name"], "address": d["address"], "vat_id": d.get("vat_id"), "bank_accounts": accounts}\n',
)
TESTS = '''import pytest

from capability import run


def test_found(fake_register):
    assert run("27082440")["name"] == "Example a.s."


def test_vat_id_optional(fake_register):
    assert run("00000001")["vat_id"] is None


def test_not_found(fake_register):
    with pytest.raises(LookupError):
        run("12345678")
'''
RED = "F..\nFAILED tests/test_unit.py::test_not_found - expected LookupError, got HTTPStatusError(404)\n1 failed, 2 passed in 2.1s"


class Run:
    """One scripted run: paced emits, a running budget, and the kill switch between steps."""

    def __init__(self, session: str, speed: float, auto: bool):
        self.log = EventLog(config.LOG_PATH, session=session)
        self.speed, self.auto = speed, auto
        self.usd = 0.0
        self.turns = 0
        self.gaps = 0
        self.started = time.monotonic()
        self._kill_offset = self.log.end()

    def emit(self, type: EventType, pause: float = 0.8, **data) -> None:
        time.sleep(pause / self.speed)
        new, self._kill_offset = self.log.read_from(self._kill_offset)
        for e in new:
            if e.type == EventType.KILL:
                raise Killed(e.data["by"])
        self.log.emit(type, fake=True, **data)

    def budget(self, turns: int = 1, usd: float = 0.02, gaps: int = 0) -> None:
        self.turns += turns
        self.gaps += gaps
        self.usd += usd
        minutes = round((time.monotonic() - self.started) * self.speed / 60 + self.turns * 0.3, 1)
        self.emit(EventType.BUDGET, 0.1, usd=round(self.usd, 3), turns=self.turns, gaps=self.gaps, minutes=minutes, limits=LIMITS)

    def test(self, ref: str, passed: bool, output: str) -> TestReport:
        report = TestReport(ref=ref, passed=passed, exit_code=0 if passed else 1, output=output, duration_s=1.9 if passed else 2.1,
                            sandbox_run_id=f"fake-{uuid.uuid4().hex[:6]}")
        self.emit(EventType.TEST_RUN, 1.5, **vars(report))
        return report

    def approve(self, manifest: dict, previous: dict | None, report: TestReport, code: str) -> bool:
        ref = f"{manifest['name']}@v{manifest['version']}"
        diff = permissions_diff(Permissions(**previous["permissions"]) if previous else None, Permissions(**manifest["permissions"]))
        req = ApprovalRequest(id=f"fake-{uuid.uuid4().hex[:8]}", ref=ref, manifest=manifest, previous=previous,
                              permissions_diff=diff, test_report=report, code=code)
        if self.auto:
            self.log.emit(EventType.APPROVAL_REQUESTED, fake=True, **vars(req))
            decide(self.log, req.id, True, by="auto-dev")
            return True
        print(f"waiting for approval of {ref} in the UI...")
        return LogApprover(self.log).request(req).approved


def task1(r: Run) -> str:
    ref = "company_lookup@v1"
    r.emit(EventType.RUN_STARTED, 0, task=TASK_1, registry=[], auth=config.AUTH)
    r.emit(EventType.PLAN, text="1. look up the company by id  2. check VAT payer status  3. answer")
    r.budget()
    r.emit(EventType.GAP, gap="look up a company by its business id", why="no installed capability returns company records",
           inputs={"ico": "string, 8 digits"}, outputs=MANIFEST["interface"]["output"], kind=Kind.CODE_TOOL, registry_search="registry empty")
    r.budget(gaps=1, usd=0.03)
    r.emit(EventType.STUDY, query="company register open data API by business id")
    r.emit(EventType.BUILD, ref=ref, role="builder", attempt=1, files=["capability.py", "manifest.yaml"],
           contents={"capability.py": CODE_V1_BROKEN})
    r.emit(EventType.BUILD, ref=ref, role="tester", attempt=1, files=["tests/test_unit.py", "tests/fixtures/ok.json"],
           contents={"tests/test_unit.py": TESTS})
    r.budget(turns=3, usd=0.41)
    r.test(ref, False, RED)
    r.emit(EventType.BUILD, ref=ref, role="repair", attempt=2, files=["capability.py"], contents={"capability.py": CODE_V1})
    r.budget(turns=2, usd=0.22)
    report = r.test(ref, True, "...\n3 passed in 1.9s")
    approved = r.approve(MANIFEST, None, report, CODE_V1)
    r.emit(EventType.INSTALL, ref=ref, installed=approved, reason="approved by operator" if approved else "rejected by operator")
    if not approved:
        return "failed"
    r.emit(EventType.CALL, call_id="call-fake01", ref=ref, ok=True, duration_s=0.4, args={"ico": "27082440"},
           output={"name": "Example a.s.", "address": "Example street 1, Prague", "vat_id": "CZ27082440"})
    r.budget()
    r.emit(EventType.ANSWER, text="Example a.s., registered at Example street 1, Prague. (fake run)", call_ids=["call-fake01"])
    return "ok"


def upgrade(r: Run) -> str:
    ref = "company_lookup@v2"
    r.emit(EventType.RUN_STARTED, 0, task=TASK_2, registry=["company_lookup@v1"], auth=config.AUTH)
    r.emit(EventType.PLAN, text="1. find installed capabilities  2. look up the supplier  3. match the invoice bank account  4. answer")
    r.budget()
    r.emit(EventType.CALL, call_id="call-fake02", ref="company_lookup@v1", ok=True, duration_s=0.4, args={"ico": "27082440"},
           output={"name": "Example a.s.", "address": "Example street 1, Prague", "vat_id": "CZ27082440"})
    r.emit(EventType.GAP, gap="list a company's published bank accounts", why="company_lookup@v1 returns no bank accounts",
           inputs={"ico": "string, 8 digits"}, outputs=MANIFEST_V2["interface"]["output"], kind=Kind.CODE_TOOL,
           registry_search="company_lookup@v1 is the closest match; upgrade it instead of building a new tool")
    r.budget(gaps=1, usd=0.03)
    r.emit(EventType.STUDY, query="published bank accounts of registered VAT payers")
    r.emit(EventType.BUILD, ref=ref, role="builder", attempt=1, files=["capability.py", "manifest.yaml"], contents={"capability.py": CODE_V2})
    r.emit(EventType.BUILD, ref=ref, role="tester", attempt=1, files=["tests/test_unit.py", "tests/test_v1_regression.py"])
    r.budget(turns=4, usd=0.52)
    report = r.test(ref, True, ".....\n5 passed in 2.4s (3 carried over from v1)")
    approved = r.approve(MANIFEST_V2, MANIFEST, report, CODE_V2)
    r.emit(EventType.INSTALL, ref=ref, installed=approved, reason="approved by operator" if approved else "rejected by operator")
    if not approved:
        r.emit(EventType.ERROR, message="upgrade rejected; company_lookup@v1 stays active and the bank account can't be verified")
        return "failed"
    r.emit(EventType.CALL, call_id="call-fake03", ref=ref, ok=True, duration_s=0.6, args={"ico": "27082440"},
           output={"name": "Example a.s.", "bank_accounts": ["123456789/0100"]})
    r.budget()
    r.emit(EventType.ANSWER, text="The invoice account 123456789/0100 is one of the supplier's published accounts. (fake run)",
           call_ids=["call-fake02", "call-fake03"])
    return "ok"


def capped(r: Run) -> str:
    ref = "rate_lookup@v1"
    r.emit(EventType.RUN_STARTED, 0, task="Convert 1 000 CZK to EUR at today's official rate.", registry=[], auth=config.AUTH)
    r.emit(EventType.PLAN, text="1. get today's exchange rate  2. convert  3. answer")
    r.budget()
    r.emit(EventType.GAP, gap="get an official daily exchange rate", why="no installed capability returns exchange rates",
           inputs={"currency": "string, ISO 4217"}, outputs={"rate": "number", "date": "string"}, kind=Kind.CODE_TOOL, registry_search="no match")
    r.budget(gaps=1, usd=0.03)
    r.emit(EventType.BUILD, ref=ref, role="builder", attempt=1, files=["capability.py", "manifest.yaml"])
    r.emit(EventType.BUILD, ref=ref, role="tester", attempt=1, files=["tests/test_unit.py"])
    r.budget(turns=3, usd=0.4)
    for attempt in range(2, MAX_REPAIRS_PER_GAP + 3):
        r.test(ref, False, "F.\nFAILED tests/test_unit.py::test_decimal_comma - could not convert string to float: '24,400'\n1 failed, 1 passed")
        if attempt - 1 > MAX_REPAIRS_PER_GAP:
            r.emit(EventType.CAP_HIT, limit="repairs_per_gap", value=attempt - 1, max=MAX_REPAIRS_PER_GAP)
            r.emit(EventType.ERROR, message=f"{ref} still fails its tests after {MAX_REPAIRS_PER_GAP} repairs; nothing was installed")
            return "capped"
        r.emit(EventType.BUILD, ref=ref, role="repair", attempt=attempt, files=["capability.py"])
        r.budget(turns=2, usd=0.25)
    return "capped"


SCENARIOS = {"task1": ("A", task1), "upgrade": ("B", upgrade), "capped": ("C", capped)}


def play(name: str, speed: float = 1.0, auto: bool = False) -> str:
    session, script = SCENARIOS[name]
    r = Run(session, speed, auto)
    try:
        status = script(r)
    except Killed:
        status = "killed"
    r.log.emit(EventType.RUN_FINISHED, fake=True, status=status)
    return status


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="task1")
    p.add_argument("--auto", action="store_true", help="approve without the UI")
    p.add_argument("--speed", type=float, default=1.0)
    a = p.parse_args()
    for name in SCENARIOS if a.scenario == "all" else [a.scenario]:
        print(f"{name}: {play(name, a.speed, a.auto)}")


if __name__ == "__main__":
    main()
