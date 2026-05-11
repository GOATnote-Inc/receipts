#!/usr/bin/env python3
"""V4 kappa regression gate.

Reads a JSONL of paired rater observations and exits 0 iff Cohen's
``kappa >= --threshold``. Wires up to the substrate stop hook
(CLAUDE.md "Stop hook gates", dual-judge kappa >= 0.40).

Input JSONL schema (one row per case):

    {"case_id": "case-001", "rater_a": 1, "rater_b": 1}
    {"case_id": "case-002", "rater_a": "yes", "rater_b": "no"}
    ...

Rater labels are treated as categorical: any JSON-hashable scalar works
(int / str / bool). Lists in file order define the paired sequences fed
into ``cohen_kappa``. The Wilson 95% CI is reported for the agreement-rate
proportion (cases where ``rater_a == rater_b``) as a small-sample sanity
band alongside the kappa point estimate.

Exit codes:
    0 -- kappa >= threshold (gate passes)
    1 -- kappa <  threshold (gate fails; reason on stderr)
    2 -- malformed input (json parse error, missing keys, type/length issue)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Hashable
from pathlib import Path

from receipts.judge import cohen_kappa, wilson_ci

_REQUIRED_FIELDS = ("case_id", "rater_a", "rater_b")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="verify_kappa",
        description="Verify Cohen's kappa >= threshold on a JSONL of rater pairs.",
    )
    p.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Path to JSONL with one {case_id, rater_a, rater_b} row per case.",
    )
    p.add_argument(
        "--threshold",
        type=float,
        default=0.40,
        help="Minimum kappa required to exit 0 (default: 0.40).",
    )
    return p.parse_args(argv)


def _load_jsonl(
    path: Path,
) -> tuple[list[Hashable], list[Hashable]]:
    """Parse a JSONL into two parallel rater label lists in file order.

    Raises:
        ValueError: malformed JSON, missing required fields, or non-hashable
            rater label.
    """
    rater_a: list[Hashable] = []
    rater_b: list[Hashable] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"verify_kappa: {path}:{lineno}: invalid JSON ({e.msg})"
                ) from e
            if not isinstance(row, dict):
                raise ValueError(
                    f"verify_kappa: {path}:{lineno}: expected object, got "
                    f"{type(row).__name__}"
                )
            for field in _REQUIRED_FIELDS:
                if field not in row:
                    raise ValueError(
                        f"verify_kappa: {path}:{lineno}: missing field {field!r}"
                    )
            a, b = row["rater_a"], row["rater_b"]
            if not isinstance(a, Hashable):
                raise ValueError(
                    f"verify_kappa: {path}:{lineno}: rater_a must be hashable"
                )
            if not isinstance(b, Hashable):
                raise ValueError(
                    f"verify_kappa: {path}:{lineno}: rater_b must be hashable"
                )
            rater_a.append(a)
            rater_b.append(b)
    return rater_a, rater_b


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)

    if not args.input.exists():
        print(
            f"verify_kappa: input file not found: {args.input}",
            file=sys.stderr,
        )
        return 2

    try:
        rater_a, rater_b = _load_jsonl(args.input)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    if not rater_a:
        print("verify_kappa: input contained no rater rows", file=sys.stderr)
        return 2

    try:
        kappa = cohen_kappa(rater_a, rater_b)
    except ValueError as e:
        # Length mismatch is impossible here (we built both lists in lockstep),
        # but an empty-input path is guarded above. Re-raise via stderr for
        # any future degenerate kappa case.
        print(f"verify_kappa: {e}", file=sys.stderr)
        return 2

    n = len(rater_a)
    agreements = sum(1 for a, b in zip(rater_a, rater_b, strict=True) if a == b)
    ci_low, ci_high = wilson_ci(agreements, n)

    print(f"kappa = {kappa:.4f} ({n} cases)")
    print(
        f"agreement = {agreements}/{n} = {agreements / n:.4f} "
        f"(Wilson 95% CI [{ci_low:.4f}, {ci_high:.4f}])"
    )

    if kappa < args.threshold:
        print(
            f"verify_kappa: kappa={kappa:.4f} below threshold {args.threshold:.4f}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
