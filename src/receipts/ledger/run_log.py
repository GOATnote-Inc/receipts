"""L3: JSONL append-only event store with fsync + thread-safe writes.

The `RunLog` class persists structured events as JSON Lines (one event per
line). Each `append()` call:
- acquires an instance lock so concurrent threads cannot interleave lines
- auto-stamps `_ts` (ISO8601 UTC) and `_seq` (monotonic int per instance)
- writes the line and `fsync()`s before releasing the lock

`replay()` reads the file from the beginning and yields events in commit
order. A missing file is treated as an empty log.

The pattern is borrowed from `scribegoat2/experiments/run_log.jsonl`
(see receipts CLAUDE.md "Reuse map") and ported to a stdlib-only class.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class RunLog:
    """Append-only JSONL event store. Thread-safe; durable via fsync."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = 0

    @property
    def path(self) -> Path:
        return self._path

    def append(self, event: dict[str, Any]) -> None:
        """Append `event` as one JSON line. Auto-stamps `_ts` and `_seq`."""
        with self._lock:
            stamped = dict(event)
            stamped["_seq"] = self._seq
            stamped["_ts"] = datetime.now(UTC).isoformat()
            line = json.dumps(stamped, separators=(",", ":")) + "\n"
            # Open/close per-append keeps semantics simple for tests that
            # inspect the file after each call; fsync guarantees durability.
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            self._seq += 1

    def replay(self) -> Iterator[dict[str, Any]]:
        """Yield every appended event in commit order."""
        if not self._path.exists():
            return
        with open(self._path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                yield json.loads(raw)


__all__ = ["RunLog"]
