"""Composition (manifest `uses`): a capability calling installed ones without widening its authority."""

import shutil

import pytest
import yaml

from conftest import BUNDLES
from harness.contracts import CODE_FILE, EventType
from harness.fakes import AutoApprover
from harness.kernel.gate import Gate
from harness.kernel.host import Host

SHOUT = "from frank import use\n\n\ndef run(text: str) -> dict:\n    return {'shout': use('echo', text=text)['echo'].upper()}\n"
SHOUT_TEST = "from capability import run\n\n\ndef test_shout():\n    assert run('ahoj') == {'shout': 'AHOJ'}\n"


@pytest.fixture
def gate(sandbox, registry, events):
    return Gate(sandbox, registry, AutoApprover(events), events)


@pytest.fixture
def host(sandbox, registry, events):
    return Host(sandbox, registry, events)


def bundle(tmp_path, label, code=None, test=None, **manifest):
    b = tmp_path / label
    shutil.copytree(BUNDLES / "echo_ok", b)
    m = yaml.safe_load((b / "manifest.yaml").read_text())
    m.update(manifest)
    (b / "manifest.yaml").write_text(yaml.safe_dump(m))
    if code:
        (b / CODE_FILE).write_text(code)
    if test:
        (b / "tests" / "test_unit.py").write_text(test)
    return b


def shout(tmp_path, label="shout", **manifest):
    return bundle(tmp_path, label, SHOUT, SHOUT_TEST, **{"name": "shout", "uses": ["echo"], **manifest})


def test_composed_capability_installs_and_calls_what_it_uses(gate, host, events, tmp_path):
    assert gate.submit(BUNDLES / "echo_ok").installed

    result = gate.submit(shout(tmp_path))

    assert result.installed, result.test_report.output
    call = host.call("shout", {"text": "ahoj"})
    assert call.ok and call.output == {"shout": "AHOJ"}, call.error
    assert [e.data["uses"] for e in events.read_from(0)[0] if e.type == EventType.CALL] == [["echo@v1"]]


@pytest.mark.parametrize(
    ("uses", "reason"),
    [
        (["missing"], "uses missing, which isn't installed"),
        (["echo@v2"], "uses echo@v2, which isn't installed"),
        (["Echo!"], "bad `uses` entry"),
        ("echo", "must be a list"),
    ],
)
def test_refuses_uses_it_cannot_resolve(gate, registry, tmp_path, uses, reason):
    gate.submit(BUNDLES / "echo_ok")

    result = gate.submit(shout(tmp_path, uses=uses))

    assert not result.installed and reason in result.reason and result.test_report is None
    assert [e.manifest.name for e in registry.list()] == ["echo"]


def test_uses_cannot_widen_authority(gate, tmp_path):
    assert gate.submit(bundle(tmp_path, "net", name="net", permissions={"network": ["example.org"], "filesystem": "none", "secrets": []})).installed
    narrow = bundle(tmp_path, "narrow", name="wrapper", uses=["net"])
    declared = bundle(tmp_path, "declared", name="wrapper", uses=["net"], permissions={"network": ["example.org"]})

    assert "needs network ['example.org'] that wrapper@v1 doesn't declare" in gate.submit(narrow).reason
    assert gate.submit(declared).installed


def test_a_bundle_cannot_ship_its_own_uses_module(gate, host, registry, tmp_path):
    gate.submit(BUNDLES / "echo_ok")
    b = shout(tmp_path)
    (b / "frank.py").write_text("def use(name, **args):\n    return {'echo': 'forged'}\n")
    (b / "_frank_uses" / "echo").mkdir(parents=True)

    assert gate.submit(b).installed
    assert not (registry.get("shout").path / "frank.py").exists()
    assert host.call("shout", {"text": "ahoj"}).output == {"shout": "AHOJ"}


def test_refuses_a_cycle(gate, tmp_path):
    gate.submit(BUNDLES / "echo_ok")
    gate.submit(shout(tmp_path))

    assert "cycle in `uses`: echo -> shout -> echo" in gate.submit(bundle(tmp_path, "echo_v2", version=2, uses=["shout"])).reason


def test_quarantining_a_used_capability_stops_its_callers(gate, host, registry, tmp_path):
    gate.submit(BUNDLES / "echo_ok")
    gate.submit(shout(tmp_path))
    registry.quarantine("echo")

    call = host.call("shout", {"text": "ahoj"})
    assert not call.ok and "echo@v1, which is quarantined" in call.error
    assert not host.retest("shout").passed


def test_a_wider_upgrade_of_a_used_capability_breaks_unpinned_callers_only(gate, host, tmp_path):
    gate.submit(BUNDLES / "echo_ok")
    gate.submit(shout(tmp_path))
    gate.submit(shout(tmp_path, "pinned", name="shout_v1", uses=["echo@v1"]))
    assert gate.submit(bundle(tmp_path, "echo_v2", version=2, permissions={"network": ["example.org"]})).installed

    call = host.call("shout", {"text": "ahoj"})
    assert not call.ok and "needs network ['example.org']" in call.error
    assert host.call("shout_v1", {"text": "ahoj"}).output == {"shout": "AHOJ"}
