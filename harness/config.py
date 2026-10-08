"""Paths and env switches. Shared.

FRANK_MODE      dev (default) | demo. Demo refuses every fake and the auto approver.
FRANK_FAKE      comma list of components to replace with fakes: sandbox,registry or "all" / "none".
                Default: sandbox,registry until stream A's real ones land, then flip the default.
FRANK_APPROVER  cli (default) | ui | auto
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = Path(os.environ.get("FRANK_LOG", ROOT / "logs" / "events.jsonl"))
REGISTRY_DIR = Path(os.environ.get("FRANK_REGISTRY_DIR", ROOT / "registry"))
WORK_DIR = Path(os.environ.get("FRANK_WORK_DIR", ROOT / "work"))  # agent build workspaces

MODE = os.environ.get("FRANK_MODE", "dev")
APPROVER = os.environ.get("FRANK_APPROVER", "cli")

_FAKEABLE = {"sandbox", "registry"}
_fake_env = os.environ.get("FRANK_FAKE", "sandbox,registry")
FAKES: set[str] = (
    _FAKEABLE if _fake_env == "all" else set() if _fake_env in ("", "none") else set(_fake_env.split(","))
)

if MODE == "demo" and (FAKES or APPROVER == "auto"):
    raise SystemExit(f"FRANK_MODE=demo forbids fakes ({sorted(FAKES)}) and the auto approver")
