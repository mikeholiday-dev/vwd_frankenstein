"""Credentials page. Owner: D.

Separate from the operator console (ui/): this is where a client enters their own
Telegram/ElevenLabs/Apify keys. `store` is a module-level singleton so the bot
process can run this alongside the Telegram bot and share the same in-memory
values (channels/telegram/bot.py mounts this app and nothing else touches it);
tests monkeypatch it the same way tests/test_console.py does for ui/app.py's
`events`. Never returns a stored value, only whether one is set.

  uv run uvicorn channels.web:app --port 8001   # standalone, for UI work on this page alone
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from channels.credentials import SERVICES, CredentialStore

app = FastAPI(title="Frankenstein credentials")
store = CredentialStore()
STATIC = Path(__file__).parent / "static"


class SetCredential(BaseModel):
    value: str
    remember: bool = False


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "credentials.html").read_text()


@app.get("/api/credentials")
def status():
    """Per service: whether something is currently usable, and whether it's remembered
    across restarts. Never the value itself."""
    remembered = store.remembered()
    return {s: {"configured": store.get(s) is not None, "remembered": s in remembered} for s in SERVICES}


@app.post("/api/credentials/{service}")
def set_credential(service: str, body: SetCredential):
    _check(service)
    store.set(service, body.value, remember=body.remember)
    return {"ok": True}


@app.delete("/api/credentials/{service}")
def forget_credential(service: str):
    _check(service)
    store.forget(service)
    return {"ok": True}


def _check(service: str) -> None:
    if service not in SERVICES:
        raise HTTPException(404, f"unknown service {service!r}")
