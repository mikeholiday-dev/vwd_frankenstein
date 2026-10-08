"""Append-only JSONL event log. Owner: C.

One file is shared by every agent process (session A, B, ...) and the UI, so
writes take an exclusive flock. Readers track a byte offset and poll; an
Event's id is the offset of its line, so ids are unique and ordered.
"""

from __future__ import annotations

import fcntl
import json
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.contracts import REQUIRED_FIELDS, Event, EventType, to_jsonable


class EventLog:
    def __init__(self, path: Path, session: str = "-", run_id: str | None = None):
        self.path = path
        self.session = session
        self.run_id = run_id or uuid.uuid4().hex[:8]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)

    def emit(self, type: EventType | str, **data: Any) -> Event:
        type = EventType(type)
        missing = [k for k in REQUIRED_FIELDS[type] if k not in data]
        if missing:
            raise ValueError(f"{type} event missing {missing}")
        data = to_jsonable(data)
        ts = datetime.now(UTC).isoformat(timespec="milliseconds")
        with self.path.open("a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                offset = f.seek(0, 2)
                record = {"id": offset, "ts": ts, "run_id": self.run_id, "session": self.session, "type": type, "data": data}
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return Event(id=offset, ts=ts, run_id=self.run_id, session=self.session, type=type, data=data)

    def read_from(self, offset: int = 0) -> tuple[list[Event], int]:
        """Complete events at or after `offset`, and the offset to resume from."""
        events = []
        with self.path.open("rb") as f:
            f.seek(offset)
            for line in f:
                if not line.endswith(b"\n"):
                    break  # partial write in progress; pick it up next time
                offset += len(line)
                r = json.loads(line)
                events.append(Event(r["id"], r["ts"], r["run_id"], r["session"], EventType(r["type"]), r["data"]))
        return events, offset

    def follow(self, offset: int = 0, poll_s: float = 0.2) -> Iterator[Event]:
        """`tail -f` over the log. Blocks forever; used by the SSE endpoint."""
        while True:
            events, offset = self.read_from(offset)
            yield from events
            if not events:
                time.sleep(poll_s)

    def end(self) -> int:
        return self.path.stat().st_size
