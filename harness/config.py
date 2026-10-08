"""Paths and env switches. Shared.

FRANK_MODE      dev (default) | demo. Demo refuses every fake and the auto approver.
FRANK_FAKE      comma list of components to replace with fakes: sandbox,registry or "all" / "none".
                Default: none (Docker sandbox + git registry). Dev without Docker: FRANK_FAKE=sandbox.
FRANK_APPROVER  cli (default) | ui | auto
FRANK_MODELS    full (default) | cheap. Cheap runs the planner and tester on Haiku and every build on Sonnet,
                for rehearsing the plumbing. Demo refuses it.
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
