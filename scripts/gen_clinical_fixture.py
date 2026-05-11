#!/usr/bin/env python3
"""P2-5: synthetic clinical-week fixture generator.

Emits four JSONL/JSON streams under ``--out`` conforming to the L1 clinical
schema (encounter / clinical_artifact / clinical_drift_finding) shipped in P2-1:

  - encounters.jsonl    -- 30 encounters with synthetic patient_id_hash values
  - artifacts.jsonl     -- 30 * 5 = 150 versioned artifacts per encounter
                          (audio -> transcript -> ai_note -> edited_note ->
                          committed_note)
  - decisions.jsonl     -- ~30 structured EHR/scribe decision-like records
  - ground_truth.json   -- per-encounter drift labels

PHI discipline:
  * patient_id_hash is the SHA-256 hex of a synthetic id like
    ``synth-patient-00001``. The plaintext id is NEVER stored.
  * Chief complaints are drawn from a curated benign word bank -- no names,
    no MRNs, no dates of birth, no SSNs.
  * Artifact bodies are not stored in-band; rows carry ``content_ref`` paths
    on a synthetic ``synth://artifacts/...`` namespace plus a ``content_hash``
    digest of the synthetic ref itself (acts as a stable per-row fingerprint).

Determinism: every random draw flows through a single ``random.Random(seed)``;
no global ``random`` state is consulted. Two runs with the same seed produce
byte-identical files.

Drift label distribution across the default 30 encounters (per spec):
  - 12 "none"
  - 9 "hallucinated-finding"
  - 5 "missing-safety-criterion"
  - 4 "dosage-error"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# -----------------------------------------------------------------------------
# Word banks: curated benign clinical-encounter vocabulary. No names, no
# numeric identifiers, no dates. The chief complaints are intentionally generic
# textbook strings -- they read like an ER triage one-liner with no PHI.
# -----------------------------------------------------------------------------

CHIEF_COMPLAINTS = (
    "chest pain",
    "shortness of breath",
    "abdominal pain",
    "headache",
    "fall",
    "fever",
    "back pain",
    "dizziness",
    "nausea and vomiting",
    "cough",
    "syncope",
    "altered mental status",
    "rash",
    "weakness",
    "palpitations",
    "sore throat",
    "ear pain",
    "knee pain",
    "ankle injury",
    "wrist injury",
    "shoulder pain",
    "flank pain",
    "urinary symptoms",
    "vision change",
    "tingling in extremities",
    "allergic reaction",
    "sinus congestion",
    "wound check",
    "medication refill",
    "follow-up visit",
)

CC_QUALIFIERS = (
    "for two days",
    "since this morning",
    "intermittent",
    "worse with exertion",
    "improving",
    "unchanged from yesterday",
    "new onset",
    "recurrent",
)

DECISION_TEMPLATES = (
    "start IV fluids and reassess in 30 minutes",
    "obtain a 12-lead EKG and basic metabolic panel",
    "administer acetaminophen for fever control",
    "consult cardiology for stress test scheduling",
    "discharge home with primary care follow-up in 48 hours",
    "admit to observation for serial troponin draws",
    "place patient on continuous cardiac monitoring",
    "image with non-contrast CT to rule out intracranial bleed",
    "trial of bronchodilator therapy, recheck oxygenation",
    "splint the affected extremity and refer to orthopedics",
)


# -----------------------------------------------------------------------------
# Drift behavior knobs.
# -----------------------------------------------------------------------------

# Expected number of safety criteria scoped per drift kind. Encounters labeled
# "missing-safety-criterion" intentionally generate a higher expected count to
# make the drift detectable: the synthetic chart-text omits at least one.
EXPECTED_SAFETY_CRITERIA_RANGE = {
    "none": (3, 5),
    "hallucinated-finding": (3, 5),
    "missing-safety-criterion": (5, 7),
    "dosage-error": (3, 5),
}

DRIFT_NOTES = {
    "none": (
        "{ext}: committed note matches transcript content; "
        "no hallucinated findings, no missing safety criteria, no dosage drift."
    ),
    "hallucinated-finding": (
        "{ext}: AI note introduces a physical-exam finding absent from audio + "
        "transcript; edits do not remove it before commit."
    ),
    "missing-safety-criterion": (
        "{ext}: at least one safety-critical criterion (allergy check, fall risk, "
        "or weight-based dosing) is absent from the committed note."
    ),
    "dosage-error": (
        "{ext}: AI note records a medication dose inconsistent with the verbal "
        "order in the transcript (wrong units or wrong magnitude)."
    ),
}


ARTIFACT_KINDS = (
    "audio",
    "transcript",
    "ai_note",
    "edited_note",
    "committed_note",
)


# -----------------------------------------------------------------------------
# Helpers.
# -----------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    """Render a UTC datetime to ISO-8601 with explicit offset."""
    return dt.astimezone(UTC).isoformat()


def _drift_label_sequence(rng: random.Random) -> list[str]:
    """Build a deterministic list of 30 drift labels in the spec distribution.

    Labels are then shuffled with the provided RNG so the ordering itself
    contributes randomness while the counts remain exact.
    """
    labels: list[str] = (
        ["none"] * 12
        + ["hallucinated-finding"] * 9
        + ["missing-safety-criterion"] * 5
        + ["dosage-error"] * 4
    )
    assert len(labels) == 30
    rng.shuffle(labels)
    return labels


def _patient_id_hash(synthetic_id: str) -> str:
    """SHA-256 hex digest of a synthetic patient id string."""
    return hashlib.sha256(synthetic_id.encode("utf-8")).hexdigest()


def _content_hash(synth_ref: str) -> str:
    """Stable per-artifact-row digest (acts as fingerprint of the synthetic ref)."""
    return hashlib.sha256(synth_ref.encode("utf-8")).hexdigest()


def _chief_complaint(rng: random.Random) -> str:
    base = rng.choice(CHIEF_COMPLAINTS)
    qualifier = rng.choice(CC_QUALIFIERS)
    return f"{base}, {qualifier}"


def _ts_in_week(rng: random.Random, week_start: datetime) -> datetime:
    """Random timestamp within the 7-day window starting at ``week_start``."""
    seconds = rng.randint(0, 7 * 24 * 3600 - 1)
    return week_start + timedelta(seconds=seconds)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            # sort_keys=True for stable byte output regardless of dict order.
            fh.write(json.dumps(row, sort_keys=True))
            fh.write("\n")


# -----------------------------------------------------------------------------
# Core generator.
# -----------------------------------------------------------------------------


def generate(
    out_dir: Path,
    *,
    seed: int = 42,
    encounters: int = 30,
) -> dict[str, int]:
    """Generate the clinical fixture set under ``out_dir`` and return counts."""

    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)

    if encounters != 30:
        # The fixed 12/9/5/4 distribution only makes sense for 30 encounters.
        raise ValueError(
            f"--encounters must be 30 for the spec drift distribution (got {encounters})"
        )

    week_start = datetime(2026, 5, 4, tzinfo=UTC)  # Monday of fixture week.
    drift_labels = _drift_label_sequence(rng)

    # --- Encounters ----------------------------------------------------------
    # Draw a shuffled pool of synthetic patient ids so the same patient
    # plausibly returns for follow-up visits but no two encounter rows reuse
    # the exact same id within a single week.
    synthetic_patient_ids = [f"synth-patient-{n:05d}" for n in range(1, encounters + 1)]
    rng.shuffle(synthetic_patient_ids)

    encounter_rows: list[dict[str, Any]] = []
    encounter_kinds: dict[str, str] = {}
    encounter_started_at: dict[str, datetime] = {}
    ground_truth: dict[str, dict[str, Any]] = {}

    for i in range(encounters):
        ext = f"ENC-{i + 1:04d}"
        started = _ts_in_week(rng, week_start)
        patient_hash = _patient_id_hash(synthetic_patient_ids[i])
        cc = _chief_complaint(rng)
        encounter_rows.append(
            {
                "external_id": ext,
                "patient_id_hash": patient_hash,
                "started_at": _iso(started),
                "chief_complaint": cc,
                "status": "closed",
            }
        )
        kind = drift_labels[i]
        encounter_kinds[ext] = kind
        encounter_started_at[ext] = started

        lo, hi = EXPECTED_SAFETY_CRITERIA_RANGE[kind]
        ground_truth[ext] = {
            "drift_kind": kind,
            "expected_safety_criteria_count": rng.randint(lo, hi),
            "notes": DRIFT_NOTES[kind].format(ext=ext),
        }

    # --- Artifacts: 5-version chain per encounter ----------------------------
    artifact_rows: list[dict[str, Any]] = []
    for ext in encounter_kinds:
        started = encounter_started_at[ext]
        for version, kind in enumerate(ARTIFACT_KINDS, start=1):
            content_ref = f"synth://artifacts/{ext}/v{version}"
            created = started + timedelta(minutes=version * 5)
            artifact_rows.append(
                {
                    "encounter_external_id": ext,
                    "kind": kind,
                    "content_ref": content_ref,
                    "content_hash": _content_hash(content_ref),
                    "version": version,
                    "parent_version": (version - 1) if version > 1 else None,
                    "created_at": _iso(created),
                }
            )

    # --- Decisions -----------------------------------------------------------
    # One structured decision per encounter, occasionally tagged to a second
    # (later) encounter to mirror "this decision applies to multiple patients
    # on the floor" scribe output.
    encounter_ids = list(encounter_kinds.keys())
    decision_rows: list[dict[str, Any]] = []
    for ext in encounter_ids:
        decision_text = rng.choice(DECISION_TEMPLATES)
        tagged = [ext]
        # ~20% chance to tag a second encounter (deterministic via rng).
        if rng.random() < 0.2:
            candidate = rng.choice(encounter_ids)
            if candidate != ext:
                tagged.append(candidate)
        confidence = round(rng.uniform(0.55, 0.99), 3)
        decision_rows.append(
            {
                "encounter_external_id": ext,
                "decision_text": decision_text,
                "tagged_encounter_external_ids": tagged,
                "confidence": confidence,
            }
        )

    # --- Write all artifacts (sorted-key JSON for byte-determinism) ---------
    _write_jsonl(out_dir / "encounters.jsonl", encounter_rows)
    _write_jsonl(out_dir / "artifacts.jsonl", artifact_rows)
    _write_jsonl(out_dir / "decisions.jsonl", decision_rows)

    # ground_truth.json: dict keyed by encounter external_id, sorted keys.
    gt_path = out_dir / "ground_truth.json"
    gt_path.write_text(
        json.dumps(ground_truth, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return {
        "encounters": len(encounter_rows),
        "artifacts": len(artifact_rows),
        "decisions": len(decision_rows),
    }


# -----------------------------------------------------------------------------
# CLI.
# -----------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path("fixtures/clinical/week_0001/"),
        help="Output directory (default: fixtures/clinical/week_0001/)",
    )
    p.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    p.add_argument(
        "--encounters",
        type=int,
        default=30,
        help="Number of encounters (default: 30)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    counts = generate(args.out, seed=args.seed, encounters=args.encounters)
    summary = ", ".join(f"{k}={v}" for k, v in counts.items())
    print(f"wrote fixture to {args.out} ({summary})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
