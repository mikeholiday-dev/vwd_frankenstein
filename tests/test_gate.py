"""Install gate hardening (plan §5): what it refuses before testing, regression tests on v2, retest."""

import json
import os
import shutil
import subprocess

import pytest
import yaml

from conftest import BUNDLES
from harness import config
from harness.contracts import CODE_FILE, EventType
from harness.fakes import AutoApprover
from harness.kernel.gate import Gate
from harness.kernel.host import Host
from harness.kernel.registry import TESTS_FILE


@pytest.fixture
def gate(sandbox, registry, events):
    return Gate(sandbox, registry, AutoApprover(events), events)


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


def logged_test_runs(events):
    return [e.data for e in events.read_from(0)[0] if e.type == EventType.TEST_RUN]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"name": "../evil"}, "must be lowercase"),
        ({"version": 2}, "version must be 1"),
        ({"version": "1"}, "positive integer"),
        ({"kind": "prompt_skill"}, "aren't supported yet"),
        ({"permissions": {"network": ["api.apify.com"], "secrets": ["APIFY_TOKEN"]}}, "isn't offered"),
    ],
)
def test_refused_before_any_test_runs(gate, registry, events, tmp_path, changes, reason):
    result = gate.submit(bundle(tmp_path, "b", **changes))

    assert not result.installed and reason in result.reason
    assert registry.list() == [] and logged_test_runs(events) == []


def test_refuses_symlinks_out_of_the_bundle(gate, events, tmp_path):
    b = bundle(tmp_path, "b")
    os.symlink(tmp_path / "secret.txt", b / "data.txt")

    result = gate.submit(b)

    assert not result.installed and "symlinks" in result.reason
    assert logged_test_runs(events) == []


def test_refuses_a_bundle_without_tests(gate, tmp_path):
    b = bundle(tmp_path, "b")
    shutil.rmtree(b / "tests")
    assert "without tests" in gate.submit(b).reason


def test_refuses_reinstalling_or_skipping_a_version(gate, tmp_path):
    assert gate.submit(bundle(tmp_path, "v1")).installed
    assert "already installed" in gate.submit(bundle(tmp_path, "v1_again")).reason
    assert "version must be 2" in gate.submit(bundle(tmp_path, "v3", version=3)).reason


def test_v2_must_pass_the_previous_versions_tests(gate, registry, events, tmp_path):
    gate.submit(bundle(tmp_path, "v1"))
    louder = bundle(
        tmp_path, "v2", version=2,
        code="def run(text: str) -> dict:\n    return {'echo': text.upper()}\n",
        test="from capability import run\n\n\ndef test_echo():\n    assert run('ahoj')['echo'] == 'AHOJ'\n",
    )  # fmt: skip

    result = gate.submit(louder)

    assert not result.installed and result.reason == "tests failed"
    assert "previous version's tests" in result.test_report.output
    assert [(t["suite"], t["passed"]) for t in logged_test_runs(events)[1:]] == [("own", True), ("regression: echo@v1 tests", False)]
    assert registry.get("echo").manifest.version == 1


def test_installs_the_snapshot_not_what_the_tests_left_behind(gate, registry, tmp_path):
    tamper = (
        "from capability import run\n\n\ndef test_echo():\n"
        "    open('capability.py', 'a').write('# tampered\\n')\n    assert run('ahoj')['echo'] == 'ahoj'\n"
    )

    assert gate.submit(bundle(tmp_path, "b", test=tamper)).installed
    assert "tampered" not in (registry.get("echo").path / CODE_FILE).read_text()


def test_retest_reruns_the_stored_tests(gate, sandbox, registry, events, tmp_path):
    gate.submit(bundle(tmp_path, "b"))
    registry.quarantine("echo")

    report = Host(sandbox, registry, events).retest("echo")

    assert report.passed and report.ref == "echo@v1"
    assert logged_test_runs(events)[-1]["suite"] == "retest"


def test_install_and_retest_leave_the_last_test_run_in_the_registry(gate, sandbox, registry, events):
    if not hasattr(registry, "record_test"):
        pytest.skip("DirRegistry keeps no test runs")
    assert gate.submit(BUNDLES / "echo_ok").installed
    runs = lambda: json.loads((registry.root / TESTS_FILE).read_text())["echo"]["1"]  # noqa: E731
    assert (runs()["passed"], runs()["suite"]) == (True, "install")

    Host(sandbox, registry, events).retest("echo")

    assert (runs()["passed"], runs()["suite"]) == (True, "retest")
    tracked = subprocess.run(["git", "ls-files"], cwd=registry.root, capture_output=True, text=True).stdout.split()
    assert TESTS_FILE not in tracked  # a test run isn't a registry change: the git log stays installs and operator actions


def test_test_dependencies_reach_test_runs_but_not_calls(gate, sandbox, registry, events, tmp_path):
    code = "import importlib.util\n\n\ndef run(text):\n    return {'echo': text, 'six': importlib.util.find_spec('six') is not None}\n"
    test = "import six  # noqa: F401\nfrom capability import run\n\n\ndef test_echo():\n    assert run('ahoj')['echo'] == 'ahoj'\n"
    b = bundle(tmp_path, "b", code=code, test=test, test_dependencies=["six==1.16.0"])

    assert gate.submit(b).installed
    assert Host(sandbox, registry, events).call("echo", {"text": "ahoj"}).output == {"echo": "ahoj", "six": False}


def test_refuses_a_secret_its_hosts_never_receive(gate, registry, events, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SECRETS", ["APIFY_TOKEN"])
    monkeypatch.setenv("APIFY_TOKEN", "k")
    result = gate.submit(bundle(tmp_path, "b", permissions={"network": ["evil.example"], "secrets": ["APIFY_TOKEN"]}))

    assert not result.installed and "never sent" in result.reason
    assert logged_test_runs(events) == []
