#!/usr/bin/env python3
"""S3: drafter fixture corpus generator.

Walks the S1 + S2 stub registries (EPIC-001..030 and ENC-001..030) and writes
30 + 30 JSON fixture files. Each fixture is a complete round-trip payload:

  fixtures/drafter/eng/epic_NNN.json
      {"epic": ..., "execution": ..., "expected_revised_spec": ...}

  fixtures/drafter/clinical/encounter_NNN.json
      {"stub": ..., "expected_contract": ...}

The ``expected_*`` field is obtained by calling the stub builder directly,
so the test suite catches regressions in either:
  - the stub itself (output drifts → golden mismatch), or
  - the validator (golden fixture stops passing → exposed contract change).

This is a *round-trip golden* design: the fixtures are not hand-edited, and
when the stub legitimately changes the operator regenerates by running:

    uv run python scripts/gen_drafter_fixtures.py

Determinism: the script reads only the stub registries, has no RNG, and
writes sorted-keys JSON. Two runs produce byte-identical output.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from receipts.drafter import (
    EncounterStub,
    Epic,
    Execution,
    MeetingRef,
    PRRef,
    ThreadRef,
    draft_encounter_contract,
    draft_revised_spec,
    validate_encounter_contract,
    validate_revised_spec,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ENG_DIR = REPO_ROOT / "fixtures" / "drafter" / "eng"
CLINICAL_DIR = REPO_ROOT / "fixtures" / "drafter" / "clinical"


# ---------------------------------------------------------------------------
# Engineering input synthesis
# ---------------------------------------------------------------------------


_ENG_EXECUTION_TEMPLATES: dict[str, dict[str, Any]] = {
    "EPIC-001": {
        "prs": [
            ("PR-101", "receipts", 101, "Add /v1/spec endpoint with epic_id query param."),
            ("PR-102", "receipts", 102, "Wire spec endpoint into CLI; add fixture corpus."),
        ],
        "meetings": [("MTG-21", ["Defer batch endpoint to next sprint."])],
        "threads": [("THR-7", "#receipts", "Confirmed CLI consumes the new endpoint.")],
    },
    "EPIC-002": {
        "prs": [("PR-201", "receipts", 201, "Implement Merkle log append with SHA-256 chain.")],
        "meetings": [("MTG-30", ["Ship as scoped; no follow-ups."])],
        "threads": [],
    },
    "EPIC-003": {
        "prs": [
            ("PR-301", "receipts", 301, "Add L0 latency budget + label emission."),
            ("PR-302", "receipts", 302, "Stamp structured failure-class label per finding."),
        ],
        "meetings": [],
        "threads": [("THR-12", "#receipts-judge", "Ambiguity in 'flags' wording resolved.")],
    },
    "EPIC-004": {
        "prs": [("PR-401", "receipts", 401, "Linear connector fixture replay harness.")],
        "meetings": [("MTG-44", ["Decision: replay fixture issues end-to-end."])],
        "threads": [],
    },
    "EPIC-005": {
        "prs": [
            ("PR-501", "receipts", 501, "Transcript ingest writes meeting row."),
            ("PR-502", "receipts", 502, "Dedup transcript ingest by external_id."),
        ],
        "meetings": [("MTG-55", ["Reviewed accidental double-writes in fixture week."])],
        "threads": [],
    },
}


def _epic_title(external_id: str) -> str:
    """Hand-crafted titles for EPIC-001..005; generic for the templated tail."""
    titles = {
        "EPIC-001": "Expose revised-spec endpoint",
        "EPIC-002": "Merkle ledger append",
        "EPIC-003": "Judge L0 deterministic scorer",
        "EPIC-004": "Linear connector shim",
        "EPIC-005": "Meeting transcript ingest",
    }
    return titles.get(external_id, f"Templated epic {external_id}")


def _epic_acceptance(external_id: str) -> list[str]:
    """Pre-revision acceptance criteria (what the team *intended* to ship)."""
    if external_id == "EPIC-001":
        return [
            "GET /v1/spec returns the latest revised spec for a given epic_id.",
            "Batch lookup for many epic_ids in a single request.",
        ]
    if external_id == "EPIC-002":
        return ["Appending an event extends the SHA-256 chain."]
    if external_id == "EPIC-003":
        return ["L0 scorer flags missing-citation patterns and emits a failure-class label."]
    if external_id == "EPIC-004":
        return ["Linear connector reads issues and round-trips fields via fixtures."]
    if external_id == "EPIC-005":
        return ["Transcript ingest writes a meeting row keyed by transcript external_id."]
    # Templated tail: a single generic criterion that the revised pair refines.
    return [f"{external_id}: feature shipped with telemetry and a kill-switch."]


def _eng_execution(external_id: str) -> Execution:
    """Build an Execution whose artifacts cover the stub's citations."""
    if external_id in _ENG_EXECUTION_TEMPLATES:
        tpl = _ENG_EXECUTION_TEMPLATES[external_id]
        prs = [
            PRRef(external_id=eid, repo=repo, number=num, diff_summary=summ)
            for (eid, repo, num, summ) in tpl["prs"]
        ]
        meetings = [
            MeetingRef(external_id=eid, decisions=list(decisions))
            for (eid, decisions) in tpl["meetings"]
        ]
        threads = [
            ThreadRef(external_id=eid, channel=chan, summary=summ)
            for (eid, chan, summ) in tpl["threads"]
        ]
        return Execution(prs=prs, meetings=meetings, threads=threads)

    # Templated tail (EPIC-006..030): execution artifact IDs are derived from
    # the epic number to match the stub's citations.
    if not external_id.startswith("EPIC-") or len(external_id) != 8:
        raise ValueError(f"unexpected eng external_id: {external_id!r}")
    nnn = int(external_id[5:])
    return Execution(
        prs=[
            PRRef(
                external_id=f"PR-{nnn:03d}01",
                repo="receipts",
                number=nnn * 100 + 1,
                diff_summary=f"Ship {external_id} feature flag + kill-switch.",
            ),
            PRRef(
                external_id=f"PR-{nnn:03d}02",
                repo="receipts",
                number=nnn * 100 + 2,
                diff_summary=f"Wire {external_id} telemetry (success + error rates).",
            ),
        ],
        meetings=[
            MeetingRef(
                external_id=f"MTG-{nnn:03d}",
                decisions=[f"{external_id}: ship behind feature flag with kill-switch documented."],
            ),
        ],
        threads=[
            ThreadRef(
                external_id=f"THR-{nnn:03d}",
                channel="#receipts-eng",
                summary=f"{external_id}: confirmed telemetry shape covers errors.",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Clinical input synthesis
# ---------------------------------------------------------------------------


_ENC_STUB_TEMPLATES: dict[str, tuple[str, list[str], str]] = {
    "ENC-001": (
        "Chest pain, 2-hour onset.",
        [
            "Substernal pressure radiating to left arm.",
            "Diaphoresis on arrival.",
            "Initial troponin 0.02 ng/mL.",
        ],
        "s3://receipts-audio/enc-001.wav",
    ),
    "ENC-002": (
        "Dysuria x 2 days.",
        ["Frequency and urgency.", "No flank pain, no fever.", "UA: positive leukocyte esterase."],
        "s3://receipts-audio/enc-002.wav",
    ),
    "ENC-003": (
        "Pediatric fever, 24-hour history.",
        ["Tachycardia for age.", "Mildly tachypneic on exam.", "No focal source on initial exam."],
        "s3://receipts-audio/enc-003.wav",
    ),
    "ENC-004": (
        "Laceration to dorsum of hand.",
        ["Clean wound margins.", "Sensation intact.", "Tetanus status uncertain."],
        "s3://receipts-audio/enc-004.wav",
    ),
    "ENC-005": (
        "Suicidal ideation.",
        ["Recent plan disclosed.", "No prior attempts.", "Family support present."],
        "s3://receipts-audio/enc-005.wav",
    ),
}


def _enc_stub(external_id: str) -> EncounterStub:
    """Build an EncounterStub matching the templated artifact IDs."""
    if external_id in _ENC_STUB_TEMPLATES:
        complaint, features, audio = _ENC_STUB_TEMPLATES[external_id]
        return EncounterStub(
            external_id=external_id,
            chief_complaint=complaint,
            presenting_features=list(features),
            audio_ref=audio,
        )

    if not external_id.startswith("ENC-") or len(external_id) != 7:
        raise ValueError(f"unexpected clinical external_id: {external_id!r}")
    nnn = int(external_id[4:])
    return EncounterStub(
        external_id=external_id,
        chief_complaint=f"{external_id}: templated presenting complaint.",
        presenting_features=[
            "Vital signs documented on arrival.",
            "Pertinent positives noted on exam.",
        ],
        audio_ref=f"s3://receipts-audio/enc-{nnn:03d}.wav",
    )


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _dump(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON with sorted keys + trailing newline for byte-determinism."""
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Per-side generators
# ---------------------------------------------------------------------------


def generate_eng(out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for n in range(1, 31):
        ext = f"EPIC-{n:03d}"
        epic = Epic(
            id=n,
            external_id=ext,
            title=_epic_title(ext),
            acceptance_criteria=_epic_acceptance(ext),
        )
        execution = _eng_execution(ext)
        spec = draft_revised_spec(epic, execution)
        # Defence-in-depth: refuse to write a fixture the validator rejects.
        validate_revised_spec(spec, epic, execution)
        # Also defensive: confirm citations only reference kinds present in
        # the Execution we just synthesized. Catches a stub citing an artifact
        # the generator forgot to wire (would later trip the test corpus).
        for cri, cites in spec.citations.items():
            for c in cites:
                kinds_avail = {
                    "pr": {r.external_id for r in execution.prs},
                    "meeting": {r.external_id for r in execution.meetings},
                    "thread": {r.external_id for r in execution.threads},
                }
                avail = kinds_avail.get(c.artifact_kind, set())
                if c.external_id not in avail:
                    raise RuntimeError(
                        f"{ext}: stub cites {c.artifact_kind}={c.external_id!r} "
                        f"for criterion {cri!r}; not present in synthesized Execution. "
                        f"Update _eng_execution() so the round-trip is well-formed."
                    )
        payload = {
            "epic": epic.model_dump(),
            "execution": execution.model_dump(),
            "expected_revised_spec": spec.model_dump(),
        }
        _dump(out_dir / f"epic_{n:03d}.json", payload)
        count += 1
    return count


def generate_clinical(out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for n in range(1, 31):
        ext = f"ENC-{n:03d}"
        stub = _enc_stub(ext)
        contract = draft_encounter_contract(stub)
        validate_encounter_contract(contract, stub)
        payload = {
            "stub": stub.model_dump(),
            "expected_contract": contract.model_dump(),
        }
        _dump(out_dir / f"encounter_{n:03d}.json", payload)
        count += 1
    return count


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--eng-out",
        type=Path,
        default=ENG_DIR,
        help=f"Output dir for engineering fixtures (default: {ENG_DIR.relative_to(REPO_ROOT)})",
    )
    p.add_argument(
        "--clinical-out",
        type=Path,
        default=CLINICAL_DIR,
        help=(f"Output dir for clinical fixtures (default: {CLINICAL_DIR.relative_to(REPO_ROOT)})"),
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    eng_count = generate_eng(args.eng_out)
    clin_count = generate_clinical(args.clinical_out)
    print(f"wrote {eng_count} eng + {clin_count} clinical drafter fixtures.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
