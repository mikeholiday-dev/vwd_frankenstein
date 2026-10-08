"""Minimal Apify client. Owner: D.

Deliberately thin: just enough to run one Actor synchronously and get its
results, through Apify's "run-sync-get-dataset-items" endpoint (no polling, no
webhook). What this is actually used for in the bot flow is still open — see
docs/WORKSTREAMS.md's stream D section. A token never reaches this module
except as an argument.
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
