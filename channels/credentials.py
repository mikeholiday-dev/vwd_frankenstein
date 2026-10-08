"""Short-lived operator credentials: Telegram/Discord/ElevenLabs/Apify keys. Owner: D.

Never written into the repo, the event log or a sandbox mount (Claude access rule in
CLAUDE.md). One `CredentialStore` lives for the process and is shared by every channel
(the bots, channels/web.py's dashboard), so a key entered without "remember" is usable
for that process's lifetime and gone when it exits. "Remember" persists it to a file
outside the repo entirely, so a later process can start already configured.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

SERVICES = ("telegram", "discord", "elevenlabs", "apify")
STORE_PATH = Path(os.environ.get("FRANK_CREDENTIALS_PATH", str(Path.home() / ".frankenstein" / "credentials.json")))


class CredentialStore:
    def __init__(self, path: Path = STORE_PATH):
        self._path = path
        self._memory: dict[str, str] = {}

    def get(self, service: str) -> str | None:
        """Env var (not remembered by us, just how the process was started), then a
        remembered or in-memory value, then None: the caller should ask the operator."""
        _check(service)
        env = os.environ.get(f"FRANK_{service.upper()}_KEY")
        if env:
            return env
        if service in self._memory:
            return self._memory[service]
        return self._read_persisted().get(service)

    def set(self, service: str, value: str, *, remember: bool = False) -> None:
        _check(service)
        self._memory[service] = value
        if remember:
            data = self._read_persisted()
            data[service] = value
            self._write_persisted(data)

    def forget(self, service: str) -> None:
        _check(service)
        self._memory.pop(service, None)
        data = self._read_persisted()
        if data.pop(service, None) is not None:
            self._write_persisted(data)

    def remembered(self) -> set[str]:
        return set(self._read_persisted())

    def _read_persisted(self) -> dict[str, str]:
        if not self._path.is_file():
            return {}
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_persisted(self, data: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data))
        self._path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600: this file holds API keys


def _check(service: str) -> None:
    if service not in SERVICES:
        raise ValueError(f"unknown service {service!r}, expected one of {SERVICES}")


# Names the kernel's vault (harness/kernel/vault.py) binds to a host and header.
VAULT_ENV_NAMES = {"apify": "APIFY_TOKEN", "elevenlabs": "ELEVENLABS_API_KEY"}


def offered_secrets(store: CredentialStore) -> dict[str, str]:
    """The operator's own keys, mapped to the env var names Frankenstein's vault (gateway
    mode) expects, for passing through to channels.runner.run_task's `secrets=`. Lets a
    channel-triggered run build and call a keyed-API capability the same way a terminal
    operator with a .env file could — separate from a channel's own direct API calls
    (e.g. channels/voice.py's ElevenLabs use for a reply)."""
    return {env_name: value for service, env_name in VAULT_ENV_NAMES.items() if (value := store.get(service))}


async def wait_for_token(store: CredentialStore, service: str, *, port: int) -> str:
    """Block until `service`'s key is available, polling the store so it also notices a
    value entered on the dashboard after the process already started — the only way to
    give a bot its own token in the first place, since there's no chat to ask in before
    it can connect at all."""
    token = store.get(service)
    if not token:
        print(f"Waiting for a {service} token: set FRANK_{service.upper()}_KEY, or open http://localhost:{port}/ and save one.")
    while not token:
        await asyncio.sleep(1.0)
        token = store.get(service)
    return token
