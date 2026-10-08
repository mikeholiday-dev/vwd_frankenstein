"""Minimal Apify client. Owner: D.

Deliberately thin: just enough to run one Actor synchronously and get its
results, through Apify's "run-sync-get-dataset-items" endpoint (no polling, no
webhook). A token never reaches this module except as an argument.

This is a separate, secondary path from the one that matters: stream A's
harness/kernel/vault.py (gateway mode) and harness/agent/tools.py's study()
are the real Apify integration — Frankenstein itself searches and can build
capabilities against it when the operator offers APIFY_TOKEN (see
channels/telegram/bot.py:offered_secrets, which passes the operator's stored
token through to a bot-triggered run exactly that way). This module would
only matter for the bot calling Apify directly, outside of a Frankenstein
run, which nothing here does yet.
"""

from __future__ import annotations

from typing import Any

import httpx

BASE_URL = "https://api.apify.com/v2"


def run_actor(actor_id: str, input: dict[str, Any], *, token: str, timeout_s: float = 120.0) -> list[dict[str, Any]]:
    """Run `actor_id` with `input` and return its dataset items. Raises on a non-2xx response."""
    url = f"{BASE_URL}/acts/{actor_id}/run-sync-get-dataset-items"
    resp = httpx.post(url, params={"token": token}, json=input, timeout=timeout_s)
    resp.raise_for_status()
    return resp.json()
