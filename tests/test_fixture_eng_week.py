"""V2 fixture tests: synthetic eng-week generator + committed week_0001 fixtures.

The generator at scripts/gen_eng_fixture.py emits five JSONL artifact streams
plus a ground_truth.json mapping epic external_ids -> drift labels. Tests cover:

1. The committed week_0001 fixture exists with all expected files.
2. The generator is byte-deterministic at seed=42.
3. ground_truth.json has the expected structure / key set.
4. The drift label distribution matches the spec (12 none / 9 creep / 5 shrink / 4 not-reflected).
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATOR = REPO_ROOT / "scripts" / "gen_eng_fixture.py"
FIXTURE_DIR = REPO_ROOT / "fixtures" / "eng" / "week_0001"

EXPECTED_FILES = (
    "epics.jsonl",
    "prs.jsonl",
    "commits.jsonl",
    "meetings.jsonl",
    "threads.jsonl",
    "ground_truth.json",
)

VALID_DRIFT_KINDS = {
    "none",
    "scope-creep",
    "scope-shrink",
    "decision-not-reflected",
}

EXPECTED_DISTRIBUTION = {
    "none": 12,
    "scope-creep": 9,
    "scope-shrink": 5,
    "decision-not-reflected": 4,
}


def _run_generator(out_dir: Path, seed: int = 42) -> None:
    cmd = [
        sys.executable,
        str(GENERATOR),
        "--out",
        str(out_dir),
        "--seed",
        str(seed),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert result.returncode == 0, (
        f"generator exited {result.returncode}:\nstdout={result.stdout}\nstderr={result.stderr}"
    )


def test_fixture_files_exist() -> None:
    """Pre-generated week_0001 fixture has all six expected files."""
    assert FIXTURE_DIR.is_dir(), f"fixture dir missing: {FIXTURE_DIR}"
    for name in EXPECTED_FILES:
        path = FIXTURE_DIR / name
        assert path.is_file(), f"expected fixture file missing: {path}"
        assert path.stat().st_size > 0, f"fixture file empty: {path}"


def test_generator_is_deterministic(tmp_path: Path) -> None:
    """Running the generator twice with seed=42 produces byte-identical outputs."""
    if not GENERATOR.is_file():
        pytest.skip(f"generator script not present: {GENERATOR}")

    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    _run_generator(out_a, seed=42)
    _run_generator(out_b, seed=42)

    for name in EXPECTED_FILES:
        bytes_a = (out_a / name).read_bytes()
        bytes_b = (out_b / name).read_bytes()
        assert bytes_a == bytes_b, f"{name} not byte-identical across seeded runs"


def test_ground_truth_structure() -> None:
    """ground_truth.json holds 30 entries keyed by epic external_id with valid drift kinds."""
    gt = json.loads((FIXTURE_DIR / "ground_truth.json").read_text())
    assert isinstance(gt, dict)
    assert len(gt) == 30, f"expected 30 ground-truth entries, got {len(gt)}"

    epic_ids = set()
    with (FIXTURE_DIR / "epics.jsonl").open() as fh:
        for line in fh:
            row = json.loads(line)
            epic_ids.add(row["external_id"])

    assert set(gt.keys()) == epic_ids, "ground_truth keys must match epic external_ids exactly"

    for eid, entry in gt.items():
        assert "drift_kind" in entry, f"{eid} missing drift_kind"
        assert entry["drift_kind"] in VALID_DRIFT_KINDS, (
            f"{eid} has invalid drift_kind: {entry['drift_kind']}"
        )
        assert "expected_pr_count" in entry, f"{eid} missing expected_pr_count"
        assert isinstance(entry["expected_pr_count"], int)
        assert entry["expected_pr_count"] >= 0
        assert "notes" in entry, f"{eid} missing notes"
        assert isinstance(entry["notes"], str)


def test_drift_distribution() -> None:
    """Drift label counts must match 12 none / 9 creep / 5 shrink / 4 not-reflected."""
    gt = json.loads((FIXTURE_DIR / "ground_truth.json").read_text())
    counts = Counter(entry["drift_kind"] for entry in gt.values())
    for kind, expected in EXPECTED_DISTRIBUTION.items():
        actual = counts.get(kind, 0)
        assert actual == expected, (
            f"drift_kind {kind!r}: expected {expected}, got {actual} (full: {dict(counts)})"
        )
    assert sum(counts.values()) == 30
