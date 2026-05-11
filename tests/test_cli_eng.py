"""P1-8: receipts-eng CLI tests.

The CLI orchestrates ``reconcile_week`` + ``emit_outputs`` against a real
in-memory ledger seeded by alembic upgrade head. It must:

* exit ``0`` on a clean dry-run against the week_0001 fixture,
* exit ``1`` when the pass^k / κ / merkle gates trip,
* exit ``2`` on argparse usage errors (missing/invalid args),
* print a one-screen summary to stdout that names the canonical fields.

We drive the CLI via ``subprocess.run`` so the test harness exercises the
exact ``python -m receipts.cli.eng`` entrypoint operators will type. No
network connectors are wired; the CLI builds connectors only when the
matching ``*_TOKEN_ENV`` variable is set in the environment.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WEEK_DIR = REPO_ROOT / "fixtures" / "eng" / "week_0001"


def _clean_env() -> dict[str, str]:
    """Environment without the four optional token vars.

    The CLI must build connectors only when the matching env var is set;
    stripping them keeps the test deterministic across operator workstations
    that may have a real ``LINEAR_API_KEY`` exported.
    """
    env = os.environ.copy()
    for key in ("LINEAR_API_KEY", "SLACK_BOT_TOKEN", "GITHUB_TOKEN", "GRANOLA_API_KEY"):
        env.pop(key, None)
    return env


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "receipts.cli.eng", *args],
        cwd=str(REPO_ROOT),
        env=_clean_env(),
        capture_output=True,
        text=True,
        check=False,
    )


def test_dry_run_returncode_zero() -> None:
    """A dry-run against the canonical week_0001 fixture must exit 0."""
    result = _run_cli(
        "run",
        "--week-fixture",
        str(WEEK_DIR),
        "--dry-run",
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_dry_run_prints_summary() -> None:
    """The dry-run summary block must surface the canonical fields."""
    result = _run_cli(
        "run",
        "--week-fixture",
        str(WEEK_DIR),
        "--dry-run",
    )
    out = result.stdout
    assert "week_id" in out
    assert "passk" in out
    assert "epic_count" in out


def test_missing_week_fixture_returncode_two() -> None:
    """A nonexistent --week-fixture path is an argparse usage error → exit 2."""
    result = _run_cli(
        "run",
        "--week-fixture",
        str(REPO_ROOT / "fixtures" / "eng" / "does_not_exist"),
        "--dry-run",
    )
    assert result.returncode == 2, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_failing_threshold_returncode_one() -> None:
    """A passk threshold above 1.0 must trip the gate → exit 1.

    The stub-backed fixture produces pass^1 == 1.0, so a threshold of 1.5
    is unreachable. The CLI must propagate that as a gate failure (exit 1),
    not a usage error (exit 2).
    """
    result = _run_cli(
        "run",
        "--week-fixture",
        str(WEEK_DIR),
        "--dry-run",
        "--passk-threshold",
        "1.5",
    )
    assert result.returncode == 1, f"stdout={result.stdout!r} stderr={result.stderr!r}"
