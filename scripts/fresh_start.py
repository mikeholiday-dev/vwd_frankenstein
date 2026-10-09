"""Move the last rehearsal aside so the next run starts from an empty registry (storyboard 0–8 s). Owner: C.

Moves the registry, the event log and the build workspaces into
rehearsals/<timestamp>[-label]/. Never deletes anything: a failed rehearsal is
evidence too. The next harness command creates a fresh, empty git registry, and
an open dashboard reloads itself when it sees the new log.

  uv run python scripts/fresh_start.py --label take-2
"""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path

from harness import config

ARCHIVE = config.ROOT / "rehearsals"


def fresh_start(label: str = "", archive: Path = ARCHIVE) -> Path | None:
    """Returns the archive folder, or None if there was nothing to move."""
    dest = archive / (datetime.now().strftime("%Y%m%d-%H%M%S") + (f"-{label}" if label else ""))
    moved = False
    for src in (config.REGISTRY_DIR, config.LOG_PATH, config.WORK_DIR):
        if src.exists():
            dest.mkdir(parents=True, exist_ok=True)
            if (dest / src.name).exists():
                raise SystemExit(f"{dest / src.name} already exists; nothing moved after it")
            shutil.move(src, dest / src.name)
            print(f"moved {src} -> {dest / src.name}")
            moved = True
    config.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.LOG_PATH.touch()
    return dest if moved else None


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--label", default="", help="appended to the archive folder name, e.g. take-2")
    a = p.parse_args()
    if fresh_start(a.label) is None:
        print("nothing to move: already a fresh start")


if __name__ == "__main__":
    main()
