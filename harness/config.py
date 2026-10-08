"""Paths and env switches. Shared.

FRANK_MODE      dev (default) | demo. Demo refuses every fake and the auto approver.
FRANK_FAKE      comma list of components to replace with fakes: sandbox,registry or "all" / "none".
                Default: none (Docker sandbox + git registry). Dev without Docker: FRANK_FAKE=sandbox.
FRANK_APPROVER  cli (default) | ui | auto
FRANK_MODELS    full (default) | cheap. Cheap runs the planner and tester on Haiku and every build on Sonnet,
                for rehearsing the plumbing. Demo refuses it.
FRANK_SECRETS   comma list of keyed-API secrets Frankenstein may use this run (e.g. APIFY_TOKEN). Default: none.

Key values (APIFY_TOKEN, ...) come from the environment or `.env` at the repo root (gitignored, see
.env.example). They stay on the host: never in the repo, the event log or a sandbox.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = Path(os.environ.get("FRANK_LOG", ROOT / "logs" / "events.jsonl"))
REGISTRY_DIR = Path(os.environ.get("FRANK_REGISTRY_DIR", ROOT / "registry"))
WORK_DIR = Path(os.environ.get("FRANK_WORK_DIR", ROOT / "work"))  # agent build workspaces

MODE = os.environ.get("FRANK_MODE", "dev")

# Claude auth. The Agent SDK uses ANTHROPIC_API_KEY when it's set, otherwise the
# local Claude Code login. Local dev and the demo run on our own subscriptions;
# anything deployed must use an API key. Recorded in every run_started event.
AUTH = "api_key" if os.environ.get("ANTHROPIC_API_KEY") else "subscription"
APPROVER = os.environ.get("FRANK_APPROVER", "cli")
MODELS = os.environ.get("FRANK_MODELS", "full")
if MODELS not in ("full", "cheap"):
    raise SystemExit(f"FRANK_MODELS must be full or cheap, not {MODELS!r}")

_FAKEABLE = {"sandbox", "registry"}
_fake_env = os.environ.get("FRANK_FAKE", "none")
FAKES: set[str] = (
    _FAKEABLE if _fake_env == "all" else set() if _fake_env in ("", "none") else set(_fake_env.split(","))
)

if MODE == "demo" and (FAKES or APPROVER == "auto" or MODELS != "full"):
    raise SystemExit(f"FRANK_MODE=demo forbids fakes ({sorted(FAKES)}), the auto approver and FRANK_MODELS={MODELS}")

ENV_FILE = ROOT / ".env"


def secret(name: str) -> str:
    """The value of `name` from the environment, else from .env, else "". Not exported to os.environ."""
    if value := os.environ.get(name, "").strip():
        return value
    try:
        lines = ENV_FILE.read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        key, sep, value = line.strip().removeprefix("export ").partition("=")
        if sep and key.strip() == name:
            return value.strip().strip("'\"")
    return ""


# Offered to the builder by name only when the operator opts in, so a key on the machine
# doesn't change how the main tasks get built. The kernel's vault decides where a key may go.
SECRETS: list[str] = [s.strip() for s in os.environ.get("FRANK_SECRETS", "").split(",") if s.strip()]
