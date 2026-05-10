"""Tests for pass^k deployment-gate metric (V3).

pass^k is the scribegoat2 deployment-gate metric: for k trials of the same
task, pass^k = fraction of tasks where ALL k trials passed. Gate at 0.95
means 95%+ of tasks must succeed on all k trials.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from receipts.judge import (
    PasskResult,
    TrialResult,
    compute_passk,
    compute_passk_detailed,
)

TOL = 1e-9

REPO_ROOT = Path(__file__).resolve().parent.parent
VERIFY_PASSK_SCRIPT = REPO_ROOT / "scripts" / "verify_passk.py"


# ----------------------------- compute_passk -----------------------------


def _all_pass_results(num_tasks: int, k: int) -> list[TrialResult]:
    out: list[TrialResult] = []
    for t in range(num_tasks):
        for trial in range(k):
            out.append(TrialResult(task_id=f"task-{t}", trial=trial, passed=True))
    return out


def test_all_pass_returns_one() -> None:
    # 3 tasks x 5 trials, every trial passes => pass^5 = 1.0.
    results = _all_pass_results(3, 5)
    assert compute_passk(results, k=5) == pytest.approx(1.0, abs=TOL)


def test_all_fail_returns_zero() -> None:
    # 3 tasks x 5 trials; each task has at least one failing trial => pass^5 = 0.0.
    results: list[TrialResult] = []
    for t in range(3):
        for trial in range(5):
            # Trial 0 always fails; remaining trials pass. Every task has a
            # failing trial, so pass^5 must be 0.0.
            results.append(TrialResult(task_id=f"task-{t}", trial=trial, passed=(trial != 0)))
    assert compute_passk(results, k=5) == pytest.approx(0.0, abs=TOL)


def test_partial() -> None:
    # 5 tasks; 4 with all 5 trials passing, 1 with a single failing trial.
    # pass^5 = 4 / 5 = 0.8.
    results: list[TrialResult] = []
    for t in range(4):
        for trial in range(5):
            results.append(TrialResult(task_id=f"task-{t}", trial=trial, passed=True))
    # Failing task: 4 passes + 1 fail.
    for trial in range(5):
        results.append(TrialResult(task_id="task-bad", trial=trial, passed=(trial != 2)))
    assert compute_passk(results, k=5) == pytest.approx(0.8, abs=TOL)


def test_excludes_wrong_k(caplog: pytest.LogCaptureFixture) -> None:
    # 2 tasks with all 5 trials passing + 1 task with only 3 trials.
    # The short task is excluded; pass^5 = 2 / 2 = 1.0.
    results = _all_pass_results(2, 5)
    for trial in range(3):
        results.append(TrialResult(task_id="task-short", trial=trial, passed=True))
    with caplog.at_level(logging.WARNING, logger="receipts.judge.passk"):
        value = compute_passk(results, k=5)
    assert value == pytest.approx(1.0, abs=TOL)
    # Warning message identifies the excluded task and the trial-count mismatch.
    assert any("task-short" in rec.message for rec in caplog.records)
    assert any(("3" in rec.message and "5" in rec.message) for rec in caplog.records)


def test_compute_passk_detailed_breakdown() -> None:
    # 3 well-formed tasks (2 all-pass, 1 has a failure) plus 1 short task
    # excluded for the wrong number of trials. passk = 2/3.
    results: list[TrialResult] = []
    for t in range(2):
        for trial in range(5):
            results.append(TrialResult(task_id=f"task-{t}", trial=trial, passed=True))
    for trial in range(5):
        results.append(TrialResult(task_id="task-fail", trial=trial, passed=(trial != 0)))
    for trial in range(2):
        results.append(TrialResult(task_id="task-short", trial=trial, passed=True))
    detailed = compute_passk_detailed(results, k=5)
    assert isinstance(detailed, PasskResult)
    assert detailed.passk == pytest.approx(2.0 / 3.0, abs=TOL)
    assert detailed.tasks_total == 3
    assert detailed.tasks_all_pass == 2
    assert detailed.tasks_excluded == 1


def test_compute_passk_raises_when_no_eligible_tasks() -> None:
    # Only a single task with the wrong number of trials -> no valid denominator.
    results = [
        TrialResult(task_id="task-short", trial=0, passed=True),
        TrialResult(task_id="task-short", trial=1, passed=True),
    ]
    with pytest.raises(ValueError, match="no tasks"):
        compute_passk(results, k=5)


def test_compute_passk_raises_on_invalid_k() -> None:
    with pytest.raises(ValueError, match="k"):
        compute_passk([], k=0)


def test_compute_passk_raises_on_duplicate_trial() -> None:
    # Same task_id + trial index twice signals data corruption upstream.
    results = [
        TrialResult(task_id="task-0", trial=0, passed=True),
        TrialResult(task_id="task-0", trial=0, passed=False),
        TrialResult(task_id="task-0", trial=1, passed=True),
        TrialResult(task_id="task-0", trial=2, passed=True),
        TrialResult(task_id="task-0", trial=3, passed=True),
        TrialResult(task_id="task-0", trial=4, passed=True),
    ]
    with pytest.raises(ValueError, match="duplicate"):
        compute_passk(results, k=5)


# -------------------------------- CLI -----------------------------------


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _run_cli(
    input_path: Path, *, threshold: float = 0.95, k: int = 5
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(VERIFY_PASSK_SCRIPT),
            "--input",
            str(input_path),
            "--threshold",
            str(threshold),
            "--k",
            str(k),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )


def test_cli_exits_1_below_threshold(tmp_path: Path) -> None:
    # 5 tasks; 3 all-pass, 2 have a failing trial. pass^5 = 0.6 < 0.95.
    rows: list[dict[str, object]] = []
    for t in range(3):
        for trial in range(5):
            rows.append({"task_id": f"task-{t}", "trial": trial, "passed": True})
    for t in range(2):
        for trial in range(5):
            rows.append(
                {
                    "task_id": f"task-bad-{t}",
                    "trial": trial,
                    "passed": (trial != 0),
                }
            )
    input_path = tmp_path / "results.jsonl"
    _write_jsonl(input_path, rows)
    completed = _run_cli(input_path, threshold=0.95, k=5)
    assert completed.returncode == 1, completed.stderr
    assert "pass^5" in completed.stdout
    assert "0.600" in completed.stdout
    assert "below threshold" in completed.stderr.lower()


def test_cli_exits_0_at_threshold(tmp_path: Path) -> None:
    # All-pass corpus, pass^5 = 1.0 >= 0.95.
    rows: list[dict[str, object]] = []
    for t in range(4):
        for trial in range(5):
            rows.append({"task_id": f"task-{t}", "trial": trial, "passed": True})
    input_path = tmp_path / "results.jsonl"
    _write_jsonl(input_path, rows)
    completed = _run_cli(input_path, threshold=0.95, k=5)
    assert completed.returncode == 0, completed.stderr
    assert "pass^5" in completed.stdout
    assert "1.000" in completed.stdout


def test_cli_warns_and_excludes_wrong_k(tmp_path: Path) -> None:
    # 3 all-pass + 1 short task (3 trials); excluded count must surface in the
    # one-line stdout summary.
    rows: list[dict[str, object]] = []
    for t in range(3):
        for trial in range(5):
            rows.append({"task_id": f"task-{t}", "trial": trial, "passed": True})
    for trial in range(3):
        rows.append({"task_id": "task-short", "trial": trial, "passed": True})
    input_path = tmp_path / "results.jsonl"
    _write_jsonl(input_path, rows)
    completed = _run_cli(input_path, threshold=0.95, k=5)
    assert completed.returncode == 0, completed.stderr
    assert "1 excluded" in completed.stdout
