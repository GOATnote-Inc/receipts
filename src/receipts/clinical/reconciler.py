"""P2-6: clinical reconciler core.

``reconcile_clinical_week`` is the single entrypoint for the
Clinical Audit Ledger weekly cycle. It runs in four phases:

1. **Ingest** — read ``encounters.jsonl`` + ``artifacts.jsonl`` from
   ``week_dir`` and populate the L1 clinical tables (``encounter`` +
   ``clinical_artifact``). Lineage between artifact versions is captured
   in-place on ``clinical_artifact.parent_artifact_id`` — the polymorphic
   ``edge`` table is NOT touched (clinical provenance is a property of
   the artifact chain, not a generic relation).
2. **Draft** — for each encounter (ordered by ``external_id`` ASC),
   assemble an ``EncounterStub`` from the encounter row + chief complaint
   + first audio artifact's ``content_ref`` and call
   :func:`draft_encounter_contract`. The S2/S3 stub registry covers
   ``ENC-001..030``; unknown IDs route to the LLM path when ``drafter_judge``
   is supplied.
3. **Validate** — every contract is fed to
   :func:`validate_encounter_contract`; pass = no exception raised.
4. **Score** — compute ``pass^1`` over the per-encounter pass/fail
   sequence. Optional ``dual_judge`` captures Cohen κ; optional
   ``hallucination_guard`` computes citation flag-rate; optional
   ``merkle_log`` chain-appends each draft and re-verifies.

Determinism contract
--------------------
- The fixture corpus uses ``ENC-0001`` (4-digit) ids; the S2/S3 stub
  registry is keyed on ``ENC-001`` (3-digit). The reconciler maps V2 →
  drafter ids via :func:`_v2_to_drafter_encounter_id` so the stub fires
  for every encounter in the week corpus.
- Drafts are emitted in ``external_id`` ASC order; Merkle hashes therefore
  chain in the same order across runs.
- Artifact rows are inserted ``(encounter_external_id, version)`` ASC so
  primary keys (and therefore ``parent_artifact_id`` linkage) are
  reproducible.

What this module deliberately does NOT do
-----------------------------------------
- No connector network calls. Scribe + FHIR connectors (P2-2 / P2-3) are
  the source of the fixture JSONL; the reconciler treats the JSONL as
  the truth.
- No emit step. FHIR Bundle / Markdown PR / PDF generation is P2-7.
- No CLI parsing. ``receipts-clin`` is P2-8.
- No clinical_drift_finding insertion. L0/L1/L2 finding rows are J7's
  responsibility; the reconciler only surfaces aggregate metrics.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from receipts.drafter import (
    EncounterContract,
    EncounterStub,
    ValidationError,
    draft_encounter_contract,
    validate_encounter_contract,
)
from receipts.judge import HallucinationGuard, TrialResult, compute_passk_detailed
from receipts.ledger.merkle import MerkleLog
from receipts.ledger.models import ClinicalArtifact, Encounter

if TYPE_CHECKING:
    from receipts.judge import DualJudge, LLMJudge


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ClinicalReconcilerResult:
    """Aggregate output of one ``reconcile_clinical_week`` invocation.

    Attributes:
        week_id: The basename of the input ``week_dir`` (e.g. ``"week_0001"``).
        drafts: ``(encounter_external_id, EncounterContract)`` pairs in
            ``external_id`` ASC order. The ``encounter_external_id`` is the
            *V2* (4-digit) id so callers can join back to the source corpus.
        encounter_count: Number of encounters processed (= ``len(drafts)``).
        pass_count: Number of contracts that passed
            :func:`validate_encounter_contract`.
        passk: ``pass^1`` computed by :func:`compute_passk_detailed` —
            ``pass_count / encounter_count``.
        kappa: Cohen κ over the dual-judge bucket sequence when a
            :class:`DualJudge` was supplied; ``None`` otherwise.
        hallucination_flag_rate: Fraction of contract citations that did
            not resolve to an artifact present in the encounter's
            artifact set, when a :class:`HallucinationGuard` was supplied;
            ``None`` otherwise.
        merkle_chain_intact: ``True`` iff a :class:`MerkleLog` was supplied
            and ``verify_chain()`` returned an empty list. When no log was
            supplied, the sentinel ``True`` is returned (the chain is
            vacuously intact because no rows were written).
        merkle_row_count: Number of Merkle rows the reconciler appended.
            ``0`` when no log was supplied.
    """

    week_id: str
    drafts: list[tuple[str, EncounterContract]] = field(default_factory=list)
    encounter_count: int = 0
    pass_count: int = 0
    passk: float = 0.0
    kappa: float | None = None
    hallucination_flag_rate: float | None = None
    merkle_chain_intact: bool = True
    merkle_row_count: int = 0


# ---------------------------------------------------------------------------
# JSONL ingest
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse a JSONL file into a list of dicts (skip blank lines)."""
    with path.open("r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _parse_dt(value: str) -> datetime:
    """Parse an ISO-8601 timestamp and drop tzinfo (DB columns are naive)."""
    return datetime.fromisoformat(value).replace(tzinfo=None)


def _ingest_clinical_week(session: Session, week_dir: Path) -> None:
    """Populate the L1 clinical tables from the week's fixture JSONL streams.

    Encounters are inserted in ``external_id`` ASC order; artifacts are
    inserted in ``(encounter_external_id, version)`` ASC order so primary
    keys are stable across runs. ``parent_artifact_id`` is resolved in a
    second pass after every artifact has its PK assigned.
    """
    encounter_rows = _read_jsonl(week_dir / "encounters.jsonl")
    artifact_rows = _read_jsonl(week_dir / "artifacts.jsonl")

    # -- Encounter ---------------------------------------------------------
    encounter_rows_sorted = sorted(encounter_rows, key=lambda r: r["external_id"])
    for row in encounter_rows_sorted:
        session.add(
            Encounter(
                external_id=row["external_id"],
                patient_id_hash=row["patient_id_hash"],
                started_at=_parse_dt(row["started_at"]),
                chief_complaint=row.get("chief_complaint", ""),
                status=row.get("status", "open"),
            )
        )
    session.flush()
    encounter_id_map: dict[str, int] = {e.external_id: e.id for e in session.query(Encounter).all()}

    # -- ClinicalArtifact --------------------------------------------------
    # First pass: insert every artifact with parent_artifact_id=None.
    # Second pass: resolve parent links via ``(encounter_id, parent_version)``
    # because the fixture encodes lineage by version, not by PK.
    artifact_rows_sorted = sorted(
        artifact_rows, key=lambda r: (r["encounter_external_id"], r["version"])
    )
    for row in artifact_rows_sorted:
        encounter_id = encounter_id_map.get(row["encounter_external_id"])
        if encounter_id is None:
            continue
        session.add(
            ClinicalArtifact(
                encounter_id=encounter_id,
                kind=row["kind"],
                content_ref=row["content_ref"],
                content_hash=row["content_hash"],
                version=row["version"],
                parent_artifact_id=None,
                created_at=_parse_dt(row["created_at"]),
            )
        )
    session.flush()

    # Build a ``(encounter_id, version) -> artifact.id`` map so we can wire
    # parent_artifact_id without round-tripping through the encounter id map.
    artifact_id_map: dict[tuple[int, int], int] = {
        (a.encounter_id, a.version): a.id for a in session.query(ClinicalArtifact).all()
    }
    for row in artifact_rows_sorted:
        parent_version = row.get("parent_version")
        if parent_version is None:
            continue
        encounter_id = encounter_id_map.get(row["encounter_external_id"])
        if encounter_id is None:
            continue
        child_id = artifact_id_map.get((encounter_id, row["version"]))
        parent_id = artifact_id_map.get((encounter_id, parent_version))
        if child_id is None or parent_id is None:
            continue
        artifact = session.get(ClinicalArtifact, child_id)
        if artifact is not None:
            artifact.parent_artifact_id = parent_id
    session.commit()


# ---------------------------------------------------------------------------
# Stub registry id mapping
# ---------------------------------------------------------------------------


def _v2_to_drafter_encounter_id(v2_external_id: str) -> str:
    """Map V2 ``ENC-0001`` (4-digit) → drafter ``ENC-001`` (3-digit).

    Mirrors :func:`receipts.eng.reconciler._v2_to_drafter_epic_id`. The
    drafter's stub registry is keyed on the 3-digit form so it stays
    compact and human-readable; the fixture corpus uses the 4-digit form
    so external systems (Linear / EHR) that emit zero-padded ids can join
    without re-numbering.
    """
    if not v2_external_id.startswith("ENC-"):
        raise ValueError(f"unexpected encounter external_id: {v2_external_id!r}")
    n = int(v2_external_id.split("-", 1)[1])
    return f"ENC-{n:03d}"


# ---------------------------------------------------------------------------
# Encounter-stub synthesis
# ---------------------------------------------------------------------------


def _split_presenting_features(chief_complaint: str) -> list[str]:
    """Split the chief complaint into presenting features.

    The fixture's chief complaints are comma-separated phrases ("ankle
    injury, worse with exertion"). Each phrase becomes a presenting
    feature so the drafter has something structured to consume. An empty
    chief complaint yields an empty list (the drafter accepts that).
    """
    if not chief_complaint:
        return []
    return [piece.strip() for piece in chief_complaint.split(",") if piece.strip()]


def _build_encounter_stub(
    encounter: Encounter,
    audio_content_ref: str,
) -> EncounterStub:
    """Assemble a drafter ``EncounterStub`` from the L1 row + audio artifact.

    The stub carries the encounter's *V2* (4-digit) external_id rather
    than the 3-digit drafter id; the drafter dispatch path is responsible
    for re-keying via :func:`_v2_to_drafter_encounter_id` when it routes
    to the stub registry.
    """
    return EncounterStub(
        external_id=encounter.external_id,
        chief_complaint=encounter.chief_complaint or "",
        presenting_features=_split_presenting_features(encounter.chief_complaint or ""),
        audio_ref=audio_content_ref,
    )


# ---------------------------------------------------------------------------
# Hallucination flag-rate
# ---------------------------------------------------------------------------


def _flag_rate(
    drafts: Iterable[tuple[str, EncounterContract]],
    encounter_artifact_refs: dict[str, set[str]],
    guard: HallucinationGuard,
) -> float:
    """Fraction of contract citations that point at phantom artifacts.

    The clinical drafter emits citations against ``transcript`` / ``note``
    / ``order`` external_ids. The reconciler verifies each cited id against
    the artifact set actually attached to that encounter — content text
    is never loaded (PHI discipline; bodies live on L5 ObjectLockStore),
    so the operational check is set-membership of ``content_ref`` keyed
    by encounter. A guard instance is accepted for API symmetry with
    :func:`receipts.eng.reconciler._flag_rate` so callers can swap in
    subclasses with different similarity thresholds.
    """
    # ``guard`` is accepted for API symmetry with the eng reconciler; the
    # content-text Jaccard path is not exercised here because the drafter
    # emits external_id citations rather than free-text quotes.
    _ = guard
    flagged = 0
    total = 0
    for ext_id, contract in drafts:
        refs = encounter_artifact_refs.get(ext_id, set())
        for citations in contract.citations.values():
            for citation in citations:
                total += 1
                if citation.external_id not in refs:
                    flagged += 1
    if total == 0:
        return 0.0
    return flagged / total


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def reconcile_clinical_week(
    week_dir: Path,
    session: Session,
    *,
    drafter_judge: LLMJudge | None = None,
    dual_judge: DualJudge | None = None,
    hallucination_guard: HallucinationGuard | None = None,
    merkle_log: MerkleLog | None = None,
) -> ClinicalReconcilerResult:
    """Reconcile one week of clinical fixtures against the L1 clinical schema.

    Args:
        week_dir: Directory holding ``encounters.jsonl`` /
            ``artifacts.jsonl`` / ``decisions.jsonl`` / ``ground_truth.json``.
        session: An already-bound SQLAlchemy ``Session`` against an L1
            schema (alembic ``head`` — applies both ``0001_init`` and
            ``0002_clinical``). The reconciler owns commit scheduling;
            callers should not hold an open transaction.
        drafter_judge: Optional ``LLMJudge`` forwarded to the drafter for
            encounters outside the S2/S3 stub registry. The stub registry
            covers ``ENC-001..030``, which is the full week_0001 corpus.
        dual_judge: Optional ``DualJudge``. When supplied, every contract
            is scored by both inner judges and the resulting Cohen κ is
            captured on the :class:`ClinicalReconcilerResult`.
        hallucination_guard: Optional ``HallucinationGuard``. When supplied,
            every drafter citation is checked against the encounter's
            artifact set and the flag-rate is reported.
        merkle_log: Optional ``MerkleLog``. When supplied, each contract is
            appended (``kind="clinical_draft"``, payload ``= contract.model_dump(mode="json")``)
            and the chain is re-verified before the result is returned.

    Returns:
        :class:`ClinicalReconcilerResult` covering ingest + draft outcomes
        plus any optional scoring that was wired in.
    """
    # ---- Step 0: ingest ------------------------------------------------
    week_dir = Path(week_dir)
    _ingest_clinical_week(session, week_dir)

    # ---- Step 1: enumerate encounters ASC -----------------------------
    encounters = session.query(Encounter).order_by(Encounter.external_id.asc()).all()

    # Pre-build the per-encounter ``content_ref`` set so the optional
    # hallucination check can answer "does this cited external_id exist on
    # this encounter?" without re-querying inside the inner loop.
    encounter_artifact_refs: dict[str, set[str]] = {}
    encounter_first_audio_ref: dict[str, str] = {}
    for enc in encounters:
        artifacts = (
            session.query(ClinicalArtifact)
            .filter(ClinicalArtifact.encounter_id == enc.id)
            .order_by(ClinicalArtifact.version.asc())
            .all()
        )
        encounter_artifact_refs[enc.external_id] = {a.content_ref for a in artifacts}
        # First version is the ``audio`` artifact per fixture contract;
        # fall back to empty ref if (somehow) the encounter has no artifacts.
        encounter_first_audio_ref[enc.external_id] = artifacts[0].content_ref if artifacts else ""

    # ---- Step 2: draft + validate -------------------------------------
    drafts: list[tuple[str, EncounterContract]] = []
    pass_count = 0
    trials: list[TrialResult] = []

    for enc in encounters:
        v2_ext_id = enc.external_id
        # The stub is keyed on the V2 id so the drafter sees the V2 form;
        # the registry dispatch handles V2 -> drafter id mapping.
        drafter_ext_id = _v2_to_drafter_encounter_id(v2_ext_id)
        stub = EncounterStub(
            external_id=drafter_ext_id,
            chief_complaint=enc.chief_complaint or "",
            presenting_features=_split_presenting_features(enc.chief_complaint or ""),
            audio_ref=encounter_first_audio_ref.get(v2_ext_id, ""),
        )

        contract = draft_encounter_contract(stub, judge=drafter_judge)
        drafts.append((v2_ext_id, contract))

        passed = True
        try:
            validate_encounter_contract(contract, stub)
        except ValidationError:
            passed = False
        if passed:
            pass_count += 1
        trials.append(TrialResult(task_id=v2_ext_id, trial=0, passed=passed))

        if merkle_log is not None:
            payload = contract.model_dump(mode="json")
            payload["encounter_external_id"] = v2_ext_id
            merkle_log.append(
                payload,
                kind="clinical_draft",
                target_id=enc.id,
                target_kind="encounter",
            )

    # ---- Step 3: pass^1 -------------------------------------------------
    passk = compute_passk_detailed(trials, k=1).passk if trials else 0.0

    # ---- Step 4: dual-judge κ (optional) -------------------------------
    kappa: float | None = None
    if dual_judge is not None:
        cases: list[tuple[str, dict[str, Any]]] = [
            (v2_ext_id, contract.model_dump(mode="json")) for v2_ext_id, contract in drafts
        ]
        kappa = dual_judge.evaluate_batch(cases).kappa

    # ---- Step 5: hallucination flag-rate (optional) --------------------
    flag_rate: float | None = None
    if hallucination_guard is not None:
        flag_rate = _flag_rate(drafts, encounter_artifact_refs, hallucination_guard)

    # ---- Step 6: Merkle verify (optional) ------------------------------
    if merkle_log is not None:
        chain_errors = merkle_log.verify_chain()
        merkle_chain_intact = chain_errors == []
        merkle_row_count = len(drafts)
    else:
        merkle_chain_intact = True
        merkle_row_count = 0

    return ClinicalReconcilerResult(
        week_id=week_dir.name,
        drafts=drafts,
        encounter_count=len(drafts),
        pass_count=pass_count,
        passk=passk,
        kappa=kappa,
        hallucination_flag_rate=flag_rate,
        merkle_chain_intact=merkle_chain_intact,
        merkle_row_count=merkle_row_count,
    )


__all__ = ["ClinicalReconcilerResult", "reconcile_clinical_week"]
