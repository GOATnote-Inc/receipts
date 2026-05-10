#!/usr/bin/env python3
"""V3 pass^k regression gate.

Reads a JSONL of per-trial pass/fail observations and exits 0 iff
``pass^k >= --threshold``. Wires up to the substrate stop hook
(CLAUDE.md "Stop hook gates").

Input JSONL schema (one row per trial):

    {"task_id": "task-001", "trial": 0, "passed": true}
    {"task_id": "task-001", "trial": 1, "passed": false}
    ...

Tasks with ``!= k`` trials are excluded with a warning and surfaced in
the stdout summary so a malformed corpus doesn't silently pass the gate.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from receipts.judge import TrialResult, compute_passk_detailed


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="verify_passk",
        description="Verify pass^k >= threshold on a JSONL of trial results.",
    )
    p.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Path to JSONL with one {task_id, trial, passed} row per trial.",
    )
    p.add_argument(
        "--threshold",
        type=float,
        default=0.95,
        help="Minimum pass^k required to exit 0 (default: 0.95).",
    )
    p.add_argument(
        "--k",
        type=int,
        default=5,
        help="Required trials per task (default: 5).",
    )
    return p.parse_args(argv)


def _load_jsonl(path: Path) -> list[TrialResult]:
    """Parse a JSONL into ``TrialResult`` objects.

    Raises:
        ValueError: malformed JSON, missing required fields, or wrong types.
    """
    results: list[TrialResult] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"verify_passk: {path}:{lineno}: invalid JSON ({e.msg})") from e
            for field in ("task_id", "trial", "passed"):
                if field not in row:
                    raise ValueError(f"verify_passk: {path}:{lineno}: missing field {field!r}")
            if not isinstance(row["task_id"], str):
                raise ValueError(f"verify_passk: {path}:{lineno}: task_id must be a string")
            if not isinstance(row["trial"], int) or isinstance(row["trial"], bool):
                raise ValueError(f"verify_passk: {path}:{lineno}: trial must be an integer")
            if not isinstance(row["passed"], bool):
                raise ValueError(f"verify_passk: {path}:{lineno}: passed must be a boolean")
            results.append(
                TrialResult(
                    task_id=row["task_id"],
                    trial=row["trial"],
                    passed=row["passed"],
                )
            )
    return results


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)

    if not args.input.exists():
        print(
            f"verify_passk: input file not found: {args.input}",
            file=sys.stderr,
        )
        return 1

    try:
        trials = _load_jsonl(args.input)
        detailed = compute_passk_detailed(trials, k=args.k)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1

    summary = (
        f"pass^{args.k} = {detailed.passk:.3f} "
        f"({detailed.tasks_all_pass}/{detailed.tasks_total} tasks; "
        f"{detailed.tasks_excluded} excluded)"
    )
    print(summary)

    if detailed.passk < args.threshold:
        print(
            f"verify_passk: pass^{args.k}={detailed.passk:.3f} below threshold "
            f"{args.threshold:.3f}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
