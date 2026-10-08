"""Egress proxy (plan §6). Owner: A.

HTTP CONNECT proxy with a per-container allowlist. No TLS interception: the
proxy only sees the CONNECT host and allows or refuses it. Every decision is
logged per token; the sandbox turns refusals into `SandboxResult.egress_denied`,
which lands in the test_run / call events: on-screen evidence for
"capabilities may grow, authority may not".

How it runs: this file, stdlib only, runs as `python /proxy.py` in a long-lived
container `frank-egress-<id>` on two docker networks: the default bridge (internet)
and SANDBOX_NET, which is `--internal` (no route out). Sandbox containers join only
SANDBOX_NET, so the proxy is their one way out. Plain HTTP is refused, except in gateway mode.

Allowlist: one file per container token in `<state>/allow/`, read on every CONNECT,
so `revoke` takes effect immediately. Entries are exact hosts ("ares.gov.cz", port
443) or "host:port". Hosts resolving to private/loopback addresses are refused.

Interface the sandbox uses:
    proxy.allow(token, domains)   # before docker run
    proxy.revoke(token)           # after the container exits
    proxy.take_log(token)         # what the container tried to reach
    proxy.env(token)              # docker flags: network + HTTPS_PROXY

Quarantine: tokens live as long as one container, and the host refuses to start a
call of a quarantined capability, so quarantine leaves it no domains. A call already
in flight (≤ MAX_SANDBOX_SECONDS) runs to the end.

Gateway mode (keyed APIs, vault.py): a capability whose token was granted a secret sends a
plain-HTTP request for `http://<host>/...` through the proxy, without the key. If the host is
in its allowlist and is one the secret is bound to, the proxy adds the key header and forwards
the request over verified HTTPS to <host>:443. No TLS interception: the capability's own
hop to the proxy stays on the internal network. The key never enters the sandbox, and the
log records the secret's name, never its value. The values reach this container as
FRANK_VAULT in its env, so a new key starts a new proxy container.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import select
import socket
import socketserver
import ssl
import subprocess
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

PORT = 3128
SANDBOX_NET = "frank-sandbox"
PACKAGE_INDEX = ["pypi.org", "files.pythonhosted.org"]  # what BUILD (and deps installs) may reach
TOKEN_TTL_S = 3600  # allow files left behind by a crashed process are swept after this


class EgressProxy:
    """Host side: starts the proxy container once per state dir and manages its allowlist."""

    _ready: set[str] = set()  # proxy containers known to be listening, per process

    def __init__(self, docker: str, cli_env: dict[str, str], image: str, user: str, state_dir: Path, vault: dict[str, dict] | None = None):
        self.docker, self.cli_env, self.image, self.user = docker, cli_env, image, user
        self.allow_dir, self.log_dir = state_dir / "allow", state_dir / "log"
        self.vault = json.dumps(vault or {}, sort_keys=True)
        key = hashlib.sha256(str(state_dir.resolve()).encode() + Path(__file__).read_bytes() + self.vault.encode()).hexdigest()[:10]
        self.name = f"frank-egress-{key}"  # new code, keys or another checkout → its own proxy
        self.address = f"{self.name}:{PORT}"

    def ensure(self) -> None:
        if self.name in self._ready:
            return
        self.allow_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        for f in self.allow_dir.iterdir():
            if f.stat().st_mtime < time.time() - TOKEN_TTL_S:
                f.unlink(missing_ok=True)
        if self._docker("inspect", "-f", "{{.State.Running}}", self.name).stdout.strip() != "true":
            self._docker("rm", "-f", self.name)
            net = self._docker("network", "create", "--internal", SANDBOX_NET)
            if net.returncode != 0 and "already exists" not in net.stderr:
                raise RuntimeError(f"docker network create failed: {net.stderr.strip()}")
            run = self._docker(
                "run", "-d", "--name", self.name, "--user", self.user, "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--memory", "256m", "--pids-limit", "256", "--label", "frank.role=egress",
                "-v", f"{Path(__file__).resolve()}:/proxy.py:ro", "-v", f"{self.allow_dir.resolve()}:/allow:ro",
                "-v", f"{self.log_dir.resolve()}:/log", "-e", "FRANK_VAULT", self.image, "python", "/proxy.py",
                env={**self.cli_env, "FRANK_VAULT": self.vault},  # by name, so the values stay off the command line
            )  # fmt: skip
            if run.returncode == 0:
                self._docker("network", "connect", SANDBOX_NET, self.name)
            elif "Conflict" not in run.stderr:  # another process started it first: fine
                raise RuntimeError(f"starting {self.name} failed: {run.stderr.strip()}")
        for _ in range(50):
            if "listening" in self._docker("logs", self.name).stdout:
                self._ready.add(self.name)
                return
            time.sleep(0.2)
        raise RuntimeError(f"{self.name} didn't start: {self._docker('logs', self.name).stderr[-1000:]}")

    def allow(self, token: str, domains: list[str], secrets: list[str] = ()) -> None:
        tmp = self.allow_dir / f".{token}.tmp"
        tmp.write_text(json.dumps({"domains": [d.lower() for d in domains], "secrets": list(secrets)}))
        tmp.rename(self.allow_dir / token)

    def revoke(self, token: str) -> None:
        (self.allow_dir / token).unlink(missing_ok=True)

    def take_log(self, token: str) -> list[dict]:
        path = self.log_dir / f"{token}.jsonl"
        if not path.exists():
            return []
        entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        path.unlink(missing_ok=True)
        return entries

    def env(self, token: str) -> list[str]:
        url = f"http://frank:{token}@{self.address}"  # token as the password: urllib drops credentials without one
        flags = ["--network", SANDBOX_NET]
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            flags += ["-e", f"{var}={url}"]
        return flags

    def _docker(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([self.docker, *args], capture_output=True, text=True, env=env or self.cli_env)


# --------------------------------------------------------------------------- #
# The proxy itself (runs inside the egress container)
# --------------------------------------------------------------------------- #


def permitted(domains: list[str], host: str, port: int) -> bool:
    for d in domains:
        h, _, p = d.partition(":")
        if h == host and int(p or 443) == port:
            return True
    return False


HOP_BY_HOP = {"proxy-authorization", "proxy-connection", "connection", "keep-alive", "te", "trailer", "upgrade"}


class _Handler(socketserver.StreamRequestHandler):
    allow_dir: Path
    log_dir: Path
    vault: dict[str, dict]

    def handle(self) -> None:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.request.recv(4096)
            if not chunk or len(head) > 16384:
                return
            head += chunk
        head, _, early = head.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        method, target = (lines[0].split(" ") + ["", ""])[:2]
        headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
        token = _token(headers.get("proxy-authorization", ""))
        if method == "CONNECT":
            host, _, port = target.rpartition(":")
            host, port = host.strip("[]").lower(), int(port) if port.isdigit() else 0
        else:  # gateway: plain HTTP in, HTTPS to port 443 out
            host, port = _gateway_target(target)

        reason, secret = self._refusal(method, token, host, port)
        upstream = None
        if not reason:
            try:
                upstream = _open(host, port, tls=bool(secret))
            except _Refused as e:
                reason = str(e)
            except OSError as e:
                reason = f"connect failed: {e}"
        self._log(token, host, port, reason, secret)
        if reason:
            body = f"frank egress: refused {host}:{port}: {reason}\n".encode()
            head = b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body)
            self.request.sendall(head + body)
            return
        with upstream:
            if secret:
                upstream.sendall(_gateway_head(method, target, lines[1:], host, self.vault[secret]) + early)
            else:
                self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                if early:
                    upstream.sendall(early)
            _pipe(self.request, upstream)

    def _refusal(self, method: str, token: str, host: str, port: int) -> tuple[str, str]:
        """(why the request is refused or "", the secret a gateway request gets)."""
        if not token:
            return "no proxy token", ""
        try:
            grant = json.loads((self.allow_dir / token).read_text())
            domains = grant["domains"]
        except (OSError, ValueError, KeyError):
            return "token unknown or revoked", ""
        secret = ""
        if method != "CONNECT":
            secret = next((n for n in grant.get("secrets", []) if host in self.vault.get(n, {}).get("hosts", [])), "")
            if not secret or not port:
                return f"only HTTPS (CONNECT) is allowed, got {method}, except to the host of a granted secret", ""
        return ("" if permitted(domains, host, port) else "not in this capability's allowlist"), secret

    def _log(self, token: str, host: str, port: int, reason: str, secret: str = "") -> None:
        entry = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "host": host, "port": port, "allowed": not reason, "reason": reason}
        if secret:
            entry["secret"] = secret  # the name only
        print(json.dumps({"token": token[:4], **entry}), flush=True)
        if token:
            with open(self.log_dir / f"{token}.jsonl", "a") as f:
                f.write(json.dumps(entry) + "\n")


class _Refused(Exception):
    pass


def _open(host: str, port: int, tls: bool) -> socket.socket:
    """Connect to a public address only; `tls` wraps it with certificate and hostname checks."""
    ip = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4][0]
    if not ipaddress.ip_address(ip).is_global:
        raise _Refused(f"resolves to non-public address {ip}")
    sock = socket.create_connection((ip, port), timeout=10)
    return ssl.create_default_context().wrap_socket(sock, server_hostname=host) if tls else sock


def _gateway_target(target: str) -> tuple[str, int]:
    """`http://host/...` → (host, 443); anything else → port 0, which nothing permits."""
    url = urllib.parse.urlsplit(target)
    try:
        ok = url.scheme == "http" and url.port in (None, 80)
    except ValueError:
        ok = False
    return (url.hostname or "").lower(), 443 if ok else 0


def _gateway_head(method: str, target: str, header_lines: list[str], host: str, secret: dict) -> bytes:
    """The capability's request in origin form, with the key added, any header of that name and hop-by-hop headers dropped."""
    url = urllib.parse.urlsplit(target)
    drop = HOP_BY_HOP | {"host", secret["header"].lower()}
    kept = [line for line in header_lines if line.partition(":")[0].strip().lower() not in drop]
    path = (url.path or "/") + (f"?{url.query}" if url.query else "")
    out = [f"{method} {path} HTTP/1.1", f"Host: {host}", *kept, f"{secret['header']}: {secret['template'].format(secret['value'])}", "Connection: close"]
    return ("\r\n".join(out) + "\r\n\r\n").encode("latin-1")


def _token(header: str) -> str:
    """`Proxy-Authorization: Basic base64(frank:token)`; tokens are hex, so they're safe as file names."""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "basic":
        return ""
    try:
        token = base64.b64decode(value).decode().partition(":")[2]
    except ValueError:
        return ""
    return token if token and all(c in "0123456789abcdef" for c in token) else ""


def _pipe(a: socket.socket, b: socket.socket) -> None:
    while True:
        # TLS may hold decrypted bytes that select() can't see
        ready = [s for s in (a, b) if isinstance(s, ssl.SSLSocket) and s.pending()] or select.select([a, b], [], [], 300)[0]
        if not ready:
            return
        for s in ready:
            data = s.recv(65536)
            if not data:
                return
            (b if s is a else a).sendall(data)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(port: int, allow_dir: Path, log_dir: Path, vault: dict[str, dict] | None = None) -> _Server:
    handler = type("Handler", (_Handler,), {"allow_dir": allow_dir, "log_dir": log_dir, "vault": vault or {}})
    return _Server(("0.0.0.0", port), handler)


if __name__ == "__main__":
    server = serve(PORT, Path("/allow"), Path("/log"), json.loads(os.environ.pop("FRANK_VAULT", "") or "{}"))
    print("listening", flush=True)
    server.serve_forever()
