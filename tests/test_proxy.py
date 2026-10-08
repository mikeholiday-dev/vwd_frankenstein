"""Egress proxy (plan §6): a capability reaches exactly the domains its manifest declares."""

import base64
import json
import socket
import threading

import pytest

from harness import config
from harness.kernel import proxy as proxy_mod
from harness.kernel import vault
from harness.kernel.proxy import permitted, serve
from harness.kernel.sandbox import DockerSandbox, DockerUnavailable

TOKEN = "ab" * 16
KEYED = "cd" * 16  # granted the secret
VAULT = {"TEST_KEY": {"hosts": ["api.example"], "header": "Authorization", "template": "Bearer {}", "value": "s3cret-value"}}


@pytest.fixture
def proxy(tmp_path):
    (tmp_path / "allow").mkdir()
    (tmp_path / "log").mkdir()
    (tmp_path / "allow" / TOKEN).write_text(json.dumps({"domains": ["example.org", "localhost", "api.example"]}))
    (tmp_path / "allow" / KEYED).write_text(json.dumps({"domains": ["api.example", "other.example"], "secrets": ["TEST_KEY"]}))
    server = serve(0, tmp_path / "allow", tmp_path / "log", VAULT)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], tmp_path / "log" / f"{TOKEN}.jsonl"
    server.shutdown()


def ask(port, request):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(request.encode())
        out = b""
        while chunk := s.recv(4096):
            out += chunk
        return out.decode()


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
    assert "403" in ask(port, connect("example.org:443", token="ef" * 16))
    assert "403" in ask(port, "GET http://example.org/ HTTP/1.1\r\nHost: example.org\r\n\r\n")


def test_allowed_name_resolving_to_a_private_address_is_refused(proxy):
    port, log = proxy
    assert "403" in ask(port, connect("localhost:443"))
    assert "non-public" in json.loads(log.read_text())["reason"]


# --- gateway mode: the proxy adds a key the capability never sees ----------- #


def gateway(url, token=KEYED, extra=""):
    auth = base64.b64encode(f"frank:{token}".encode()).decode()
    return f"GET {url} HTTP/1.1\r\nHost: api.example\r\nProxy-Authorization: Basic {auth}\r\nAuthorization: Bearer forged\r\n{extra}\r\n"


@pytest.fixture
def upstream(monkeypatch):
    """Stands in for the HTTPS service: answers with the request it got."""
    srv = socket.create_server(("127.0.0.1", 0))
    opened = []

    def answer():
        try:
            conn, _ = srv.accept()
        except OSError:  # closed unused: the proxy refused before connecting
            return
        with conn:
            got = b""
            while b"\r\n\r\n" not in got:
                got += conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: %d\r\n\r\n" % len(got) + got)

    threading.Thread(target=answer, daemon=True).start()

    def fake_open(host, port, tls):
        opened.append((host, port, tls))
        return socket.create_connection(srv.getsockname())

    monkeypatch.setattr(proxy_mod, "_open", fake_open)
    yield opened
    srv.close()


def test_gateway_adds_the_key_over_tls_and_drops_proxy_auth(proxy, upstream):
    port, _ = proxy
    reply = ask(port, gateway("http://api.example/v2/run?x=1", extra="Proxy-Connection: keep-alive\r\n"))
    assert reply.startswith("HTTP/1.1 200")
    request = reply.partition("\r\n\r\n")[2]
    assert request.startswith("GET /v2/run?x=1 HTTP/1.1\r\n")
    assert "Authorization: Bearer s3cret-value" in request and "forged" not in request
    assert "proxy-" not in request.lower()
    assert upstream == [("api.example", 443, True)]


def test_gateway_logs_the_secret_name_never_the_value(proxy, upstream, tmp_path):
    port, _ = proxy
    ask(port, gateway("http://api.example/"))
    log = (tmp_path / "log" / f"{KEYED}.jsonl").read_text()
    assert json.loads(log)["secret"] == "TEST_KEY" and "s3cret" not in log


@pytest.mark.parametrize(
    ("url", "token"),
    [
        ("http://other.example/", KEYED),  # allowed host, but not the secret's
        ("http://api.example/", TOKEN),  # the secret's host, but no secret granted
        ("http://api.example:8080/", KEYED),  # only port 80 in, 443 out
        ("https://api.example/", KEYED),  # absolute https: the capability must not hold the key, so no
    ],
)
def test_gateway_refuses_anything_but_a_granted_secrets_host(proxy, upstream, url, token):
    port, _ = proxy
    assert "403" in ask(port, gateway(url, token=token))
    assert upstream == []


def test_gateway_needs_the_host_in_the_allowlist_too(tmp_path, upstream):
    (tmp_path / "allow").mkdir()
    (tmp_path / "log").mkdir()
    (tmp_path / "allow" / KEYED).write_text(json.dumps({"domains": [], "secrets": ["TEST_KEY"]}))
    server = serve(0, tmp_path / "allow", tmp_path / "log", VAULT)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert "403" in ask(server.server_address[1], gateway("http://api.example/"))
    finally:
        server.shutdown()
    assert upstream == []


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


ECHO = "postman-echo.com"  # answers with the request headers it got


@pytest.fixture
def keyed_docker(tmp_path, monkeypatch):
    monkeypatch.setitem(vault.BINDINGS, "FRANK_TEST_KEY", vault.Binding((ECHO,), "X-Frank-Key", "k={}"))
    monkeypatch.setattr(config, "SECRETS", ["FRANK_TEST_KEY"])
    monkeypatch.setenv("FRANK_TEST_KEY", "not-a-real-key")
    try:
        return DockerSandbox(registry_dir=tmp_path / "registry")
    except DockerUnavailable as e:
        pytest.skip(f"no docker here: {e}")


def test_gateway_end_to_end_key_reaches_its_host_but_not_the_sandbox(keyed_docker, tmp_path):
    code = (
        "import json, os, urllib.request as u; "
        f"h = json.load(u.urlopen(u.Request('http://{ECHO}/headers', headers={{'User-Agent': 'frank-test'}}), timeout=20))['headers']; "
        "print(h.get('x-frank-key'), h.get('x-forwarded-proto'), 'not-a-real-key' in str(os.environ))"
    )
    r = keyed_docker.run(tmp_path, ["python", "-c", code], phase="call", network=[ECHO], secrets=["FRANK_TEST_KEY"])
    if "HTTP Error 5" in r.stderr:
        pytest.skip(f"{ECHO} is down")
    assert r.exit_code == 0, r.stderr
    assert r.stdout.split() == ["k=not-a-real-key", "https", "False"]

    plain = keyed_docker.run(tmp_path, ["python", "-c", code], phase="call", network=[ECHO])
    assert plain.exit_code != 0 and plain.egress_denied == [f"{ECHO}:443"]


def test_secrets_not_offered_or_in_build_phase_never_reach_the_proxy(keyed_docker, tmp_path):
    r = keyed_docker.run(tmp_path, ["true"], phase="call", network=[ECHO], secrets=["APIFY_TOKEN"])
    assert r.exit_code != 0 and "not offered" in r.stderr
    assert keyed_docker.run(tmp_path, ["true"], phase="build", secrets=["APIFY_TOKEN"]).exit_code == 0
