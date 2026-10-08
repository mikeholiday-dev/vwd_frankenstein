"""Egress proxy (plan §6). Owner: A.

HTTP CONNECT proxy with a per-container allowlist. No TLS interception: the
proxy only sees the CONNECT host and allows or refuses it. Log every refusal:
it's on-screen evidence for "capabilities may grow, authority may not".

Interface the sandbox uses:
    proxy.allow(token, domains)   # before docker run
    proxy.revoke(token)           # after the container exits
    proxy.address                 # "host:port" reachable from containers

Quarantine (UI) must also drop the capability's domains: revoke tokens for it.
Gateway mode (inject keys for keyed APIs) is stretch, only for the ElevenLabs prize.
"""

from __future__ import annotations


class EgressProxy:
    address: str = ""

    def allow(self, token: str, domains: list[str]) -> None:
        raise NotImplementedError("stream A")

    def revoke(self, token: str) -> None:
        raise NotImplementedError("stream A")
