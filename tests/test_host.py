"""Capability host: the operator's input files reach calls, read-only, and nothing else."""

import pytest

from harness.fakes import AutoApprover
from harness.kernel.gate import Gate
from harness.kernel.host import INPUTS_DIR, Host
from test_compose import bundle

READ = "def run(path: str) -> dict:\n    with open(path, 'rb') as f:\n        return {'text': f.read().decode()}\n"
READ_TEST = (
    "from capability import run\n\n\n"
    "def test_reads(tmp_path):\n    (tmp_path / 'a.txt').write_text('x')\n    assert run(str(tmp_path / 'a.txt')) == {'text': 'x'}\n"
)


@pytest.fixture
def host(sandbox, registry, events, tmp_path):
    gate = Gate(sandbox, registry, AutoApprover(events), events)
    b = bundle(tmp_path, "reader", READ, READ_TEST, name="reader")
    (b / INPUTS_DIR).mkdir()
    (b / INPUTS_DIR / "invoice.txt").write_text("planted by the bundle")
    assert gate.submit(b).installed
    return Host(sandbox, registry, events)


def test_a_call_reads_an_attached_file(host, tmp_path):
    (tmp_path / "invoice.txt").write_text("Faktura č. 1")

    [path] = host.attach(tmp_path / "invoice.txt")
    call = host.call("reader", {"path": path})

    assert path == f"{INPUTS_DIR}/invoice.txt"
    assert call.ok, call.error
    assert call.output == {"text": "Faktura č. 1"}


def test_a_bundle_cannot_plant_its_own_inputs(host):
    call = host.call("reader", {"path": f"{INPUTS_DIR}/invoice.txt"})

    assert not call.ok
    assert "FileNotFoundError" in call.error


def test_attach_refuses_what_it_cannot_hand_over(host, tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "x.pdf").write_text("1")
    (tmp_path / "b" / "x.pdf").write_text("2")

    with pytest.raises(FileNotFoundError):
        host.attach(tmp_path / "missing.pdf")
    with pytest.raises(FileNotFoundError):
        host.attach(tmp_path / "a")
    host.attach(tmp_path / "a" / "x.pdf")
    with pytest.raises(ValueError):
        host.attach(tmp_path / "b" / "x.pdf")
