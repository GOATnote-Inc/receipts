"""L3: run_log append-only event-store tests.

`RunLog` is a JSONL append-only event store with fsync-on-append. Each event
is auto-stamped with:
- `_ts`: ISO8601 UTC timestamp string
- `_seq`: monotonically increasing integer (per RunLog instance)

The instance is thread-safe via an internal lock so parallel writers can call
`append()` without interleaving JSON lines. `replay()` yields events back in
the order they were appended.

Tests:
- single-writer roundtrip
- parallel writers (10 threads x 100 events) -> 1000 events, no corruption,
  per-thread `_seq` values are monotonic in their dispatch order
- replay yields events in append order
- replay on empty / never-written file -> empty iterator
- auto-stamped `_ts` is ISO8601 parseable; `_seq` is int
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from receipts.ledger.run_log import RunLog


def test_append_replay_roundtrip(tmp_path: Path) -> None:
    log = RunLog(tmp_path / "events.jsonl")
    for i in range(10):
        log.append({"i": i, "kind": "tick"})

    out = list(log.replay())
    assert len(out) == 10
    for i, ev in enumerate(out):
        assert ev["i"] == i
        assert ev["kind"] == "tick"
        assert "_ts" in ev
        assert "_seq" in ev


def test_parallel_writers(tmp_path: Path) -> None:
    log = RunLog(tmp_path / "events.jsonl")

    n_threads = 10
    per_thread = 100

    def worker(tid: int) -> None:
        for j in range(per_thread):
            log.append({"tid": tid, "j": j})

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    out = list(log.replay())
    assert len(out) == n_threads * per_thread

    # Every event must round-trip cleanly (no torn lines).
    for ev in out:
        assert isinstance(ev, dict)
        assert "tid" in ev and "j" in ev
        assert isinstance(ev["_seq"], int)
        assert isinstance(ev["_ts"], str)

    # No lost rows: each (tid, j) appears exactly once.
    pairs = {(ev["tid"], ev["j"]) for ev in out}
    assert len(pairs) == n_threads * per_thread

    # Per-thread, j values appear in their original dispatch order (FIFO).
    # Because append() takes a lock and worker dispatches j=0,1,2,..., the
    # subsequence of events for a given tid must have j monotonically rising.
    for tid in range(n_threads):
        js = [ev["j"] for ev in out if ev["tid"] == tid]
        assert js == sorted(js), f"thread {tid} j-order not monotonic: {js[:10]}..."
        # And _seq must be monotonic in that same per-thread order.
        seqs = [ev["_seq"] for ev in out if ev["tid"] == tid]
        assert seqs == sorted(seqs), f"thread {tid} _seq not monotonic"

    # Global _seq is dense from 0 to N-1 (or 1 to N — implementation chooses).
    all_seqs = sorted(ev["_seq"] for ev in out)
    assert all_seqs == list(range(all_seqs[0], all_seqs[0] + len(all_seqs)))


def test_replay_order_matches_append(tmp_path: Path) -> None:
    log = RunLog(tmp_path / "events.jsonl")
    payloads = [{"label": f"e{i}", "n": i} for i in range(25)]
    for p in payloads:
        log.append(p)

    out = list(log.replay())
    assert [ev["label"] for ev in out] == [p["label"] for p in payloads]
    assert [ev["_seq"] for ev in out] == sorted(ev["_seq"] for ev in out)


def test_replay_empty(tmp_path: Path) -> None:
    # Never-written file: replay should yield nothing.
    log = RunLog(tmp_path / "never_written.jsonl")
    assert list(log.replay()) == []


def test_auto_stamps_present(tmp_path: Path) -> None:
    log = RunLog(tmp_path / "events.jsonl")
    log.append({"hello": "world"})

    raw = (tmp_path / "events.jsonl").read_text().splitlines()
    assert len(raw) == 1
    ev = json.loads(raw[0])
    assert ev["hello"] == "world"
    assert isinstance(ev["_seq"], int)
    assert isinstance(ev["_ts"], str)

    # _ts must parse as ISO8601 and end in UTC marker.
    parsed = datetime.fromisoformat(ev["_ts"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None


def test_append_auto_creates_parent_dirs(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c" / "events.jsonl"
    log = RunLog(nested)
    log.append({"x": 1})
    assert nested.exists()
    assert list(log.replay())[0]["x"] == 1
