"""Egress proxy (plan §6): a capability reaches exactly the domains its manifest declares."""

import base64
import json
import socket
import threading

import pytest

from harness.kernel.proxy import permitted, serve
from harness.kernel.sandbox import DockerSandbox, DockerUnavailable

TOKEN = "ab" * 16


@pytest.fixture
def proxy(tmp_path):
    (tmp_path / "allow").mkdir()
    (tmp_path / "log").mkdir()
    (tmp_path / "allow" / TOKEN).write_text(json.dumps({"domains": ["example.org", "localhost"]}))
    server = serve(0, tmp_path / "allow", tmp_path / "log")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], tmp_path / "log" / f"{TOKEN}.jsonl"
    server.shutdown()


def ask(port, request):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(request.encode())
        return s.recv(4096).decode()


def connect(host, token=TOKEN):
    auth = base64.b64encode(f"frank:{token}".encode()).decode()
    return f"CONNECT {host} HTTP/1.1\r\nHost: {host}\r\nProxy-Authorization: Basic {auth}\r\n\r\n"


def test_allowlist_is_exact_host_and_port():
    assert permitted(["ares.gov.cz"], "ares.gov.cz", 443)
    assert not permitted(["ares.gov.cz"], "evil.ares.gov.cz", 443)
    assert not permitted(["ares.gov.cz"], "ares.gov.cz", 80)
    assert permitted(["ares.gov.cz:8443"], "ares.gov.cz", 8443)


def test_refuses_hosts_outside_the_allowlist_and_logs_it(proxy):
    port, log = proxy
    assert "403" in ask(port, connect("evil.example:443"))
    entry = json.loads(log.read_text())
    assert (entry["host"], entry["port"], entry["allowed"]) == ("evil.example", 443, False)
    assert entry["reason"] == "not in this capability's allowlist"


def test_refuses_missing_or_unknown_tokens_and_plain_http(proxy):
    port, _ = proxy
    assert "403" in ask(port, "CONNECT example.org:443 HTTP/1.1\r\n\r\n")
    assert "403" in ask(port, connect("example.org:443", token="cd" * 16))
    assert "403" in ask(port, "GET http://example.org/ HTTP/1.1\r\nHost: example.org\r\n\r\n")


def test_allowed_name_resolving_to_a_private_address_is_refused(proxy):
    port, log = proxy
    assert "403" in ask(port, connect("localhost:443"))
    assert "non-public" in json.loads(log.read_text())["reason"]


# --- through the real sandbox (needs Docker and internet) ------------------- #


@pytest.fixture
def docker(tmp_path):
    try:
        return DockerSandbox(registry_dir=tmp_path / "registry")
    except DockerUnavailable as e:
        pytest.skip(f"no docker here: {e}")


def fetch(url):  # example.org answers 403 to urllib's default User-Agent
    code = f"import urllib.request as u; print(u.urlopen(u.Request({url!r}, headers={{'User-Agent': 'frank-test'}}), timeout=10).status)"
    return ["python", "-c", code]


def test_call_reaches_its_manifest_domain_only(docker, tmp_path):
    ok = docker.run(tmp_path, fetch("https://example.org/"), phase="call", network=["example.org"])
    assert (ok.exit_code, ok.stdout.strip(), ok.egress_denied) == (0, "200", []), ok.stderr

    denied = docker.run(tmp_path, fetch("https://example.com/"), phase="call", network=["example.org"])
    assert denied.exit_code != 0
    assert denied.egress_denied == ["example.com:443"]
    assert "[egress] refused example.com:443" in denied.stderr


def test_requests_honours_the_proxy(docker, tmp_path):
    code = "import requests; print(requests.get('https://example.org/', timeout=10).status_code)"
    r = docker.run(tmp_path, ["python", "-c", code], phase="test", network=["example.org"], deps=["requests==2.32.3"])
    assert (r.exit_code, r.stdout.strip()) == (0, "200"), r.stderr


def test_build_phase_reaches_the_package_index_only(docker, tmp_path):
    assert docker.run(tmp_path, fetch("https://pypi.org/simple/six/"), phase="build").exit_code == 0
    r = docker.run(tmp_path, fetch("https://example.org/"), phase="build", network=["example.org"])
    assert r.egress_denied == ["example.org:443"]


def test_token_is_revoked_after_the_run(docker, tmp_path):
    docker.run(tmp_path, ["true"], phase="call", network=["example.org"])
    assert list(docker.proxy.allow_dir.iterdir()) == []
