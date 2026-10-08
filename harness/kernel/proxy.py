"""Egress proxy (plan §6). Owner: A.

HTTP CONNECT proxy with a per-container allowlist. No TLS interception: the
proxy only sees the CONNECT host and allows or refuses it. Every decision is
logged per token; the sandbox turns refusals into `SandboxResult.egress_denied`,
which lands in the test_run / call events: on-screen evidence for
"capabilities may grow, authority may not".

How it runs: this file, stdlib only, runs as `python /proxy.py` in a long-lived
container `frank-egress-<id>` on two docker networks: the default bridge (internet)
and SANDBOX_NET, which is `--internal` (no route out). Sandbox containers join only
SANDBOX_NET, so the proxy is their one way out. Plain HTTP is refused: CONNECT only.

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
Gateway mode (inject keys for keyed APIs) is stretch, only for the ElevenLabs prize.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import select
import socket
import socketserver
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

PORT = 3128
SANDBOX_NET = "frank-sandbox"
PACKAGE_INDEX = ["pypi.org", "files.pythonhosted.org"]  # what BUILD (and deps installs) may reach
TOKEN_TTL_S = 3600  # allow files left behind by a crashed process are swept after this


class EgressProxy:
    """Host side: starts the proxy container once per state dir and manages its allowlist."""

    _ready: set[str] = set()  # proxy containers known to be listening, per process

    def __init__(self, docker: str, cli_env: dict[str, str], image: str, user: str, state_dir: Path):
        self.docker, self.cli_env, self.image, self.user = docker, cli_env, image, user
        self.allow_dir, self.log_dir = state_dir / "allow", state_dir / "log"
        key = hashlib.sha256(str(state_dir.resolve()).encode() + Path(__file__).read_bytes()).hexdigest()[:10]
        self.name = f"frank-egress-{key}"  # new code or another checkout → its own proxy
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
                "-v", f"{self.log_dir.resolve()}:/log", self.image, "python", "/proxy.py",
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

    def allow(self, token: str, domains: list[str]) -> None:
        tmp = self.allow_dir / f".{token}.tmp"
        tmp.write_text(json.dumps({"domains": [d.lower() for d in domains]}))
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

    def _docker(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([self.docker, *args], capture_output=True, text=True, env=self.cli_env)


# --------------------------------------------------------------------------- #
# The proxy itself (runs inside the egress container)
# --------------------------------------------------------------------------- #


def permitted(domains: list[str], host: str, port: int) -> bool:
    for d in domains:
        h, _, p = d.partition(":")
        if h == host and int(p or 443) == port:
            return True
    return False


class _Handler(socketserver.StreamRequestHandler):
    allow_dir: Path
    log_dir: Path

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
        host, _, port = target.rpartition(":") if method == "CONNECT" else (target, "", "0")
        host = host.strip("[]").lower()
        port = int(port) if port.isdigit() else 0

        reason = self._refusal(method, token, host, port)
        upstream = None
        if not reason:
            try:
                ip = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4][0]
                if not ipaddress.ip_address(ip).is_global:
                    reason = f"resolves to non-public address {ip}"
                else:
                    upstream = socket.create_connection((ip, port), timeout=10)
            except OSError as e:
                reason = f"connect failed: {e}"
        self._log(token, host, port, reason)
        if reason:
            body = f"frank egress: refused {host}:{port}: {reason}\n".encode()
            head = b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body)
            self.request.sendall(head + body)
            return
        with upstream:
            self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if early:
                upstream.sendall(early)
            _pipe(self.request, upstream)

    def _refusal(self, method: str, token: str, host: str, port: int) -> str:
        if method != "CONNECT":
            return f"only HTTPS (CONNECT) is allowed, got {method}"
        if not token:
            return "no proxy token"
        try:
            domains = json.loads((self.allow_dir / token).read_text())["domains"]
        except (OSError, ValueError, KeyError):
            return "token unknown or revoked"
        return "" if permitted(domains, host, port) else "not in this capability's allowlist"

    def _log(self, token: str, host: str, port: int, reason: str) -> None:
        entry = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "host": host, "port": port, "allowed": not reason, "reason": reason}
        print(json.dumps({"token": token[:4], **entry}), flush=True)
        if token:
            with open(self.log_dir / f"{token}.jsonl", "a") as f:
                f.write(json.dumps(entry) + "\n")


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
        ready, _, _ = select.select([a, b], [], [], 300)
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


def serve(port: int, allow_dir: Path, log_dir: Path) -> _Server:
    handler = type("Handler", (_Handler,), {"allow_dir": allow_dir, "log_dir": log_dir})
    return _Server(("0.0.0.0", port), handler)


if __name__ == "__main__":
    server = serve(PORT, Path("/allow"), Path("/log"))
    print("listening", flush=True)
    server.serve_forever()
