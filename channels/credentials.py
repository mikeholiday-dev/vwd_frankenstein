"""Short-lived operator credentials: Telegram/ElevenLabs/Apify keys. Owner: D.

Never written into the repo, the event log or a sandbox mount (Claude access rule in
CLAUDE.md). One `CredentialStore` lives for the process and is shared by the bot and
its embedded credentials page (channels/web.py), so a key entered without "remember"
is usable for that process's lifetime and gone when it exits. "Remember" persists it
to a file outside the repo entirely, so a later process can start already configured.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

SERVICES = ("telegram", "elevenlabs", "apify")
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
