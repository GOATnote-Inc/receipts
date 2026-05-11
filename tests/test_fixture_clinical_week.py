"""P2-5 fixture tests: synthetic clinical-week generator + committed week_0001 fixtures.

The generator at scripts/gen_clinical_fixture.py emits four artifacts:

  - encounters.jsonl    -- 30 encounters with synthetic patient_id_hash values
  - artifacts.jsonl     -- 30 * 5 = 150 versioned artifacts per encounter
                          (audio -> transcript -> ai_note -> edited_note ->
                          committed_note)
  - decisions.jsonl     -- ~30 structured EHR/scribe decision-like records
  - ground_truth.json   -- per-encounter drift labels with the spec
                          distribution: 12 none / 9 hallucinated-finding /
                          5 missing-safety-criterion / 4 dosage-error.

The fixture conforms to the L1 clinical schema (encounter / clinical_artifact /
clinical_drift_finding) shipped in P2-1. Patient identifiers are NEVER real:
patient_id_hash is the SHA-256 of a synthetic id (e.g. ``synth-patient-00001``)
and chief complaints are drawn from a curated benign word bank.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATOR = REPO_ROOT / "scripts" / "gen_clinical_fixture.py"
FIXTURE_DIR = REPO_ROOT / "fixtures" / "clinical" / "week_0001"

EXPECTED_FILES = (
    "encounters.jsonl",
    "artifacts.jsonl",
    "decisions.jsonl",
    "ground_truth.json",
)

VALID_DRIFT_KINDS = {
    "none",
    "hallucinated-finding",
    "missing-safety-criterion",
    "dosage-error",
}

EXPECTED_DISTRIBUTION = {
    "none": 12,
    "hallucinated-finding": 9,
    "missing-safety-criterion": 5,
    "dosage-error": 4,
}

EXPECTED_ARTIFACT_KINDS = (
    "audio",
    "transcript",
    "ai_note",
    "edited_note",
    "committed_note",
)


# PHI sentinels: nine-digit SSN-like, ten-plus-digit MRN-like, real-DOB-shaped
# (e.g. 01/15/1972, 1972-01-15). These are precisely what a real chart text
# would contain; the synthetic chief complaints must contain none of them.
PHI_PATTERNS = (
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN
    re.compile(r"\bMRN[:\s]*\d{6,}\b", re.IGNORECASE),  # MRN
    re.compile(r"\b\d{1,2}/\d{1,2}/(19|20)\d{2}\b"),  # US DOB
    re.compile(r"\b(19|20)\d{2}-\d{2}-\d{2}\b"),  # ISO DOB
    re.compile(r"\bDOB[:\s]"),  # explicit DOB labels
)


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


def _read_encounters() -> list[dict]:
    rows: list[dict] = []
    with (FIXTURE_DIR / "encounters.jsonl").open() as fh:
        for line in fh:
            rows.append(json.loads(line))
    return rows


def _read_artifacts() -> list[dict]:
    rows: list[dict] = []
    with (FIXTURE_DIR / "artifacts.jsonl").open() as fh:
        for line in fh:
            rows.append(json.loads(line))
    return rows


def test_fixture_files_exist() -> None:
    """Pre-generated week_0001 fixture has all four expected files."""
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
    """ground_truth.json holds 30 entries keyed by encounter external_id with valid drift kinds."""
    gt = json.loads((FIXTURE_DIR / "ground_truth.json").read_text())
    assert isinstance(gt, dict)
    assert len(gt) == 30, f"expected 30 ground-truth entries, got {len(gt)}"

    enc_ids = {row["external_id"] for row in _read_encounters()}
    assert set(gt.keys()) == enc_ids, "ground_truth keys must match encounter external_ids exactly"

    for eid, entry in gt.items():
        assert "drift_kind" in entry, f"{eid} missing drift_kind"
        assert entry["drift_kind"] in VALID_DRIFT_KINDS, (
            f"{eid} has invalid drift_kind: {entry['drift_kind']}"
        )
        assert "expected_safety_criteria_count" in entry, (
            f"{eid} missing expected_safety_criteria_count"
        )
        assert isinstance(entry["expected_safety_criteria_count"], int)
        assert entry["expected_safety_criteria_count"] >= 0
        assert "notes" in entry, f"{eid} missing notes"
        assert isinstance(entry["notes"], str)


def test_drift_distribution_counts() -> None:
    """Drift label counts must match 12 none / 9 hallucinated / 5 missing-safety / 4 dosage."""
    gt = json.loads((FIXTURE_DIR / "ground_truth.json").read_text())
    counts = Counter(entry["drift_kind"] for entry in gt.values())
    for kind, expected in EXPECTED_DISTRIBUTION.items():
        actual = counts.get(kind, 0)
        assert actual == expected, (
            f"drift_kind {kind!r}: expected {expected}, got {actual} (full: {dict(counts)})"
        )
    assert sum(counts.values()) == 30


def test_artifacts_form_five_version_chains() -> None:
    """Each of the 30 encounters has exactly the 5-kind version chain."""
    rows = _read_artifacts()
    assert len(rows) == 30 * 5, f"expected 150 artifact rows, got {len(rows)}"

    by_encounter: dict[str, list[dict]] = {}
    for row in rows:
        by_encounter.setdefault(row["encounter_external_id"], []).append(row)

    assert len(by_encounter) == 30, (
        f"expected 30 distinct encounters in artifacts, got {len(by_encounter)}"
    )

    for ext, chain in by_encounter.items():
        kinds = tuple(r["kind"] for r in sorted(chain, key=lambda r: r["version"]))
        assert kinds == EXPECTED_ARTIFACT_KINDS, (
            f"{ext} artifact-chain kinds {kinds} != expected {EXPECTED_ARTIFACT_KINDS}"
        )
        versions = [r["version"] for r in sorted(chain, key=lambda r: r["version"])]
        assert versions == [1, 2, 3, 4, 5], f"{ext} versions {versions} != [1..5]"
        # Parent chain: v1 has null parent, v2..v5 point at v-1.
        sorted_chain = sorted(chain, key=lambda r: r["version"])
        assert sorted_chain[0]["parent_version"] is None, f"{ext} v1 must have null parent_version"
        for i in range(1, 5):
            assert sorted_chain[i]["parent_version"] == i, (
                f"{ext} v{i + 1} parent_version must be {i}, got {sorted_chain[i]['parent_version']}"
            )
        # Each row must carry a 64-char SHA-256 hex content_hash and synth:// ref.
        for row in chain:
            assert isinstance(row["content_hash"], str) and len(row["content_hash"]) == 64
            assert all(c in "0123456789abcdef" for c in row["content_hash"])
            assert row["content_ref"].startswith("synth://"), (
                f"content_ref must be synthetic: {row['content_ref']}"
            )


def test_no_real_phi_in_chief_complaints() -> None:
    """Chief complaints contain no SSN-like, MRN-like, or real-DOB-shaped tokens."""
    rows = _read_encounters()
    assert len(rows) == 30
    for row in rows:
        cc = row["chief_complaint"]
        assert isinstance(cc, str) and cc, f"{row['external_id']} chief_complaint must be non-empty"
        for pattern in PHI_PATTERNS:
            assert pattern.search(cc) is None, (
                f"{row['external_id']} chief_complaint matched PHI pattern "
                f"{pattern.pattern!r}: {cc!r}"
            )


def test_patient_id_hash_is_sha256_of_synthetic_id() -> None:
    """Each encounter's patient_id_hash must be SHA-256 hex of a synth-patient-NNNNN id."""
    rows = _read_encounters()
    for row in rows:
        h = row["patient_id_hash"]
        assert isinstance(h, str)
        assert len(h) == 64 and all(c in "0123456789abcdef" for c in h), (
            f"{row['external_id']} patient_id_hash {h!r} is not a 64-char hex digest"
        )
    # At least one encounter's hash must match SHA-256 of synth-patient-00001..00030
    # (we don't pin which encounter -> which synth-id, only that the population
    # is drawn from the synthetic pool, never from real identifiers).
    synthetic_pool = {
        hashlib.sha256(f"synth-patient-{n:05d}".encode()).hexdigest() for n in range(1, 1001)
    }
    observed = {row["patient_id_hash"] for row in rows}
    assert observed.issubset(synthetic_pool), (
        f"patient_id_hash values include non-synthetic digests: {observed - synthetic_pool}"
    )


def test_decisions_jsonl_shape() -> None:
    """decisions.jsonl carries ~30 lines, each tagged to a real encounter."""
    rows: list[dict] = []
    with (FIXTURE_DIR / "decisions.jsonl").open() as fh:
        for line in fh:
            rows.append(json.loads(line))
    assert 1 <= len(rows) <= 60, f"expected ~30 decisions, got {len(rows)}"
    enc_ids = {r["external_id"] for r in _read_encounters()}
    for row in rows:
        assert "encounter_external_id" in row
        assert row["encounter_external_id"] in enc_ids
        assert "decision_text" in row and isinstance(row["decision_text"], str)
        assert "tagged_encounter_external_ids" in row
        assert isinstance(row["tagged_encounter_external_ids"], list)
        for tagged in row["tagged_encounter_external_ids"]:
            assert tagged in enc_ids
        assert "confidence" in row
        assert isinstance(row["confidence"], float)
        assert 0.0 <= row["confidence"] <= 1.0
