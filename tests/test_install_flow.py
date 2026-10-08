"""Kernel acceptance: gate + host over any Sandbox/Registry pair (plan §9, hour 0–2 exit check)."""

import shutil

import yaml

from conftest import BUNDLES
from harness.contracts import ApprovalDecision, EventType
from harness.fakes import AutoApprover
from harness.kernel.gate import Gate
from harness.kernel.host import Host


class Reject:
    def __init__(self, events):
        self.events = events

    def request(self, req):
        return ApprovalDecision(req.id, False, "test", "no")


def types(events):
    return [e.type for e in events.read_from(0)[0]]


def test_passing_bundle_installs_and_runs_without_secrets(sandbox, registry, events, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
    gate = Gate(sandbox, registry, AutoApprover(events), events)

    result = gate.submit(BUNDLES / "echo_ok")

    assert result.installed, result.test_report.output
    assert [e.manifest.ref for e in registry.list()] == ["echo@v1"]
    call = Host(sandbox, registry, events).call("echo", {"text": "ahoj"})
    assert call.ok, call.error
    assert call.output == {"echo": "ahoj", "sees_api_key": False}
    assert types(events) == [EventType.TEST_RUN, EventType.APPROVAL_REQUESTED, EventType.APPROVAL_DECIDED, EventType.INSTALL, EventType.CALL]


def test_failing_tests_never_reach_the_operator(sandbox, registry, events):
    result = Gate(sandbox, registry, AutoApprover(events), events).submit(BUNDLES / "echo_broken")

    assert not result.installed
    assert "1 failed" in result.test_report.output
    assert registry.list() == []
    assert EventType.APPROVAL_REQUESTED not in types(events)


def test_rejected_bundle_is_not_installed(sandbox, registry, events):
    result = Gate(sandbox, registry, Reject(events), events).submit(BUNDLES / "echo_ok")

    assert not result.installed
    assert registry.list() == []


def test_upgrade_shows_permissions_diff_and_rolls_back(sandbox, registry, events, tmp_path):
    gate = Gate(sandbox, registry, AutoApprover(events), events)
    gate.submit(BUNDLES / "echo_ok")
    v2 = tmp_path / "echo_v2"
    shutil.copytree(BUNDLES / "echo_ok", v2)
    m = yaml.safe_load((v2 / "manifest.yaml").read_text())
    m["version"], m["permissions"]["network"] = 2, ["example.org"]
    (v2 / "manifest.yaml").write_text(yaml.safe_dump(m))

    assert gate.submit(v2).installed
    req = next(e for e in events.read_from(0)[0] if e.type == EventType.APPROVAL_REQUESTED and e.data["ref"] == "echo@v2")
    assert req.data["permissions_diff"] == {"added": {"network": ["example.org"]}, "removed": {}}
    assert req.data["previous"]["version"] == 1

    assert registry.rollback("echo", 1).manifest.version == 1
    assert registry.get("echo").manifest.version == 1
    assert registry.get("echo", 2).manifest.version == 2


def test_quarantined_capability_cannot_be_called(sandbox, registry, events):
    Gate(sandbox, registry, AutoApprover(events), events).submit(BUNDLES / "echo_ok")
    registry.quarantine("echo")

    assert registry.list() == []
    assert not Host(sandbox, registry, events).call("echo", {"text": "x"}).ok
