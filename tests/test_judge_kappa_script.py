"""Tests for V4 kappa regression gate CLI (``scripts/verify_kappa.py``).

The stop-hook gate (CLAUDE.md "Stop hook gates") requires Cohen's κ ≥ 0.40
on the dual-judge fixture. This script is the parallel of V3's pass^k
regression script.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERIFY_KAPPA_SCRIPT = REPO_ROOT / "scripts" / "verify_kappa.py"


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _run_cli(
    input_path: Path, *, threshold: float | None = None
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(VERIFY_KAPPA_SCRIPT), "--input", str(input_path)]
    if threshold is not None:
        cmd += ["--threshold", str(threshold)]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )


def test_perfect_agreement_passes(tmp_path: Path) -> None:
    # 10 identical pairs => kappa = 1.0, well above the 0.40 default gate.
    rows: list[dict[str, object]] = [
        {"case_id": f"case-{i}", "rater_a": (i % 2), "rater_b": (i % 2)} for i in range(10)
    ]
    input_path = tmp_path / "perfect.jsonl"
    _write_jsonl(input_path, rows)
    completed = _run_cli(input_path)
    assert completed.returncode == 0, completed.stderr
    assert "kappa" in completed.stdout.lower()
    assert "10 cases" in completed.stdout
    # Perfect agreement renders as 1.0000 (or 1.000) in the rounded output.
    assert "1.0000" in completed.stdout or "1.000" in completed.stdout


def test_low_agreement_fails(tmp_path: Path) -> None:
    # 8/10 disagree => raters mostly invert each other. Marginals stay
    # balanced (5/5 vs 5/5), so P_e = 0.5 and kappa = (0.2 - 0.5)/0.5 = -0.6,
    # well below the 0.40 default threshold.
    a_labels = [1, 1, 1, 1, 1, 0, 0, 0, 0, 0]
    b_labels = [0, 0, 0, 0, 1, 1, 1, 1, 1, 0]
    rows: list[dict[str, object]] = [
        {"case_id": f"case-{i}", "rater_a": a_labels[i], "rater_b": b_labels[i]} for i in range(10)
    ]
    input_path = tmp_path / "low.jsonl"
    _write_jsonl(input_path, rows)
    completed = _run_cli(input_path)
    assert completed.returncode == 1, completed.stdout
    assert "threshold" in completed.stderr.lower()
    # Reason should mention the actual kappa and the threshold.
    assert "0.40" in completed.stderr or "0.400" in completed.stderr


def test_threshold_override_arg(tmp_path: Path) -> None:
    # Same low-kappa fixture, but with --threshold 0.0 the negative kappa
    # still fails (kappa=-0.6 < 0.0). Use a fixture with kappa in (0.0, 0.4)
    # to verify the override gate actually lets borderline-but-positive
    # agreement through.
    # Construction: 6/10 agree, marginals balanced 5/5 -> P_o=0.6, P_e=0.5,
    # kappa=0.2. Fails default 0.40, passes --threshold 0.0.
    a_labels = [1, 1, 1, 1, 1, 0, 0, 0, 0, 0]
    b_labels = [1, 1, 1, 0, 0, 1, 1, 0, 0, 0]
    rows: list[dict[str, object]] = [
        {"case_id": f"case-{i}", "rater_a": a_labels[i], "rater_b": b_labels[i]} for i in range(10)
    ]
    input_path = tmp_path / "borderline.jsonl"
    _write_jsonl(input_path, rows)
    # Default threshold (0.40): fails.
    failed = _run_cli(input_path)
    assert failed.returncode == 1, failed.stdout
    # Lowered threshold: passes.
    passed = _run_cli(input_path, threshold=0.0)
    assert passed.returncode == 0, passed.stderr
    assert "kappa" in passed.stdout.lower()


def test_invalid_jsonl_returncode_2(tmp_path: Path) -> None:
    # Malformed JSON line => exit 2 (input error, distinct from gate failure).
    input_path = tmp_path / "bad.jsonl"
    input_path.write_text(
        '{"case_id": "ok", "rater_a": 1, "rater_b": 1}\nthis is not json\n',
        encoding="utf-8",
    )
    completed = _run_cli(input_path)
    assert completed.returncode == 2, (completed.stdout, completed.stderr)
    assert completed.stderr.strip() != ""


def test_missing_field_returncode_2(tmp_path: Path) -> None:
    # Missing rater_b key on a row => exit 2.
    input_path = tmp_path / "missing.jsonl"
    rows: list[dict[str, object]] = [
        {"case_id": "case-0", "rater_a": 1, "rater_b": 1},
        {"case_id": "case-1", "rater_a": 0},
    ]
    _write_jsonl(input_path, rows)
    completed = _run_cli(input_path)
    assert completed.returncode == 2, (completed.stdout, completed.stderr)
    assert "rater_b" in completed.stderr or "missing" in completed.stderr.lower()


def test_stdout_includes_wilson_ci(tmp_path: Path) -> None:
    # 8/10 agree on the same label, marginals shared. The script should
    # surface a Wilson 95% CI line for the agreement-rate proportion in
    # addition to the kappa line.
    a_labels = [1, 1, 1, 1, 1, 0, 0, 0, 0, 0]
    b_labels = [1, 1, 1, 1, 1, 0, 0, 1, 1, 0]  # 8/10 agree
    rows: list[dict[str, object]] = [
        {"case_id": f"case-{i}", "rater_a": a_labels[i], "rater_b": b_labels[i]} for i in range(10)
    ]
    input_path = tmp_path / "wilson.jsonl"
    _write_jsonl(input_path, rows)
    completed = _run_cli(input_path, threshold=0.0)
    assert completed.returncode == 0, completed.stderr
    lower = completed.stdout.lower()
    assert "wilson" in lower or "95%" in lower or "ci" in lower
