"""Keys for keyed APIs (plan §3, gateway mode). Owner: A.

A capability never sees a key. Its manifest names the secret (`permissions.secrets`), the
operator approves that on the card, and the egress proxy adds the key to the requests the
capability sends to that secret's host. The team binds each secret here to the hosts it may
be sent to and the header that carries it, so whatever a manifest declares, a key only ever
goes to its own service.

A secret is usable in a run when it is bound here, has a value (config.secret: the env or
`.env`) and the operator offered it (FRANK_SECRETS). Values go only to the proxy container.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from harness import config
from harness.contracts import Permissions


@dataclass(frozen=True)
class Binding:
    hosts: tuple[str, ...]  # exact hosts the key may be sent to, over HTTPS
    header: str
    template: str = "{}"


BINDINGS: dict[str, Binding] = {
    "APIFY_TOKEN": Binding(("api.apify.com",), "Authorization", "Bearer {}"),
    "ELEVENLABS_API_KEY": Binding(("api.elevenlabs.io",), "xi-api-key"),
}


def offered() -> list[str]:
    """Secrets capabilities may use in this run."""
    return [n for n in config.SECRETS if n in BINDINGS and config.secret(n)]


def problem(permissions: Permissions) -> str:
    """Why these secrets can't be granted, or "". Names no hosts: the agent has to find its APIs itself."""
    available = offered()
    for name in permissions.secrets:
        if name not in available:
            return f"secret {name!r} isn't offered in this run (offered: {available or 'none'})"
        if not set(BINDINGS[name].hosts) & set(permissions.network):
            return f"secret {name!r} is never sent to any host in permissions.network"
    return ""


def for_proxy() -> dict[str, dict]:
    """What the proxy container gets: the offered secrets with their values and bindings."""
    return {n: {**asdict(BINDINGS[n]), "value": config.secret(n)} for n in offered()}
