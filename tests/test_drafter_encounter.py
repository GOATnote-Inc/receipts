"""Tests for the clinical encounter-contract drafter + validator (S2).

The drafter consumes an EncounterStub (chief complaint + presenting features +
audio reference) and emits an EncounterContract — the clinical analog of the
engineering RevisedSpec. Where engineering tracks intent-vs-execution drift on
PRs/meetings/threads, the clinical side tracks it on transcripts/notes/orders
and requires explicit safety_criteria.

S2 validator contracts:

  (a) every criterion (acceptance OR safety) is cited by at least one Citation.
  (b) at least one safety_criterion exists (clinical encounters require a
      defined safety floor).
  (c) every Citation references an artifact_kind in {"transcript","note","order"}.
  (d) the EncounterContract is a valid pydantic v2 model.

The LLM is stubbed: draft_encounter_contract dispatches on stub.external_id
against a small hand-written lookup (ENC-001..005). Unknown stubs raise
NotImplementedError, matching the S1 pattern.
"""

from __future__ import annotations

import pytest

from receipts.drafter import (
    Citation,
    EncounterContract,
    EncounterStub,
    ValidationError,
    draft_encounter_contract,
    validate_encounter_contract,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _stub_enc_001() -> EncounterStub:
    """ENC-001 — chest pain workup with drift: patient declined troponin recheck."""
    return EncounterStub(
        external_id="ENC-001",
        chief_complaint="Chest pain, 2-hour onset.",
        presenting_features=[
            "Substernal pressure radiating to left arm.",
            "Diaphoresis on arrival.",
            "Initial troponin 0.02 ng/mL.",
        ],
        audio_ref="s3://receipts-audio/enc-001.wav",
    )


def _stub_enc_002() -> EncounterStub:
    """ENC-002 — uncomplicated UTI, no drift."""
    return EncounterStub(
        external_id="ENC-002",
        chief_complaint="Dysuria x 2 days.",
        presenting_features=[
            "Frequency and urgency.",
            "No flank pain, no fever.",
            "Urinalysis: positive leukocyte esterase.",
        ],
        audio_ref="s3://receipts-audio/enc-002.wav",
    )


# ---------------------------------------------------------------------------
# Drafter tests
# ---------------------------------------------------------------------------


def test_draft_encounter_returns_contract_with_drift() -> None:
    """ENC-001 has drift; drift_summary must be non-trivial and reference it."""
    stub = _stub_enc_001()

    contract = draft_encounter_contract(stub)

    assert isinstance(contract, EncounterContract)
    assert contract.external_id == "ENC-001"
    assert len(contract.acceptance_criteria) >= 1
    assert len(contract.safety_criteria) >= 1
    assert contract.drift_summary  # non-empty
    # Drift case must say something substantive (not the "no drift" canned reply).
    lowered = contract.drift_summary.lower()
    assert "no drift" not in lowered
    assert len(contract.drift_summary) >= 20  # non-trivial
    # Every criterion must have at least one citation.
    for criterion in contract.acceptance_criteria + contract.safety_criteria:
        assert criterion in contract.citations
        assert len(contract.citations[criterion]) >= 1
    # Validator must accept the drafter's own output.
    validate_encounter_contract(contract, stub)


def test_draft_encounter_returns_contract_no_drift() -> None:
    """ENC-002 shipped as scoped; drift_summary indicates no drift."""
    stub = _stub_enc_002()

    contract = draft_encounter_contract(stub)

    assert isinstance(contract, EncounterContract)
    assert contract.external_id == "ENC-002"
    assert len(contract.safety_criteria) >= 1
    assert contract.drift_summary
    lowered = contract.drift_summary.lower()
    assert ("no drift" in lowered) or ("as scoped" in lowered) or ("no change" in lowered)
    validate_encounter_contract(contract, stub)


def test_draft_encounter_unknown_stub_raises_not_implemented() -> None:
    """Stub returns NotImplementedError for unknown encounter ids."""
    stub = EncounterStub(
        external_id="ENC-UNKNOWN",
        chief_complaint="not in lookup",
        presenting_features=["something"],
        audio_ref="s3://receipts-audio/missing.wav",
    )
    with pytest.raises(NotImplementedError):
        draft_encounter_contract(stub)


# ---------------------------------------------------------------------------
# Validator tests
# ---------------------------------------------------------------------------


def test_validator_passes_on_well_cited_contract() -> None:
    """Hand-crafted EncounterContract with citations + safety criterion passes."""
    stub = _stub_enc_002()
    acc = "Diagnosis recorded with ICD-10 code in the chart."
    safety = "Allergy reconciliation completed before antibiotic order."
    contract = EncounterContract(
        external_id="ENC-002",
        acceptance_criteria=[acc],
        safety_criteria=[safety],
        citations={
            acc: [Citation(artifact_kind="note", external_id="NOTE-002")],
            safety: [Citation(artifact_kind="order", external_id="ORD-002")],
        },
        drift_summary="Shipped as scoped — no drift.",
    )
    assert validate_encounter_contract(contract, stub) is None


def test_validator_fails_on_empty_safety_criteria() -> None:
    """Clinical encounters require ≥1 safety criterion — empty list is rejected."""
    stub = _stub_enc_002()
    acc = "Diagnosis recorded with ICD-10 code in the chart."
    contract = EncounterContract(
        external_id="ENC-002",
        acceptance_criteria=[acc],
        safety_criteria=[],  # empty — rejected
        citations={
            acc: [Citation(artifact_kind="note", external_id="NOTE-002")],
        },
        drift_summary="Shipped as scoped.",
    )
    with pytest.raises(ValidationError):
        validate_encounter_contract(contract, stub)


def test_validator_fails_on_missing_criterion_citation() -> None:
    """A criterion (acceptance or safety) without any citation is rejected."""
    stub = _stub_enc_002()
    acc = "Diagnosis recorded with ICD-10 code in the chart."
    safety = "Allergy reconciliation completed before antibiotic order."
    contract = EncounterContract(
        external_id="ENC-002",
        acceptance_criteria=[acc],
        safety_criteria=[safety],
        citations={
            acc: [Citation(artifact_kind="note", external_id="NOTE-002")],
            # safety criterion is missing from citations — rejected
        },
        drift_summary="Shipped as scoped.",
    )
    with pytest.raises(ValidationError):
        validate_encounter_contract(contract, stub)


def test_validator_fails_on_phantom_artifact_kind() -> None:
    """Citation with an artifact_kind outside {transcript,note,order} is rejected.

    We force a phantom kind by constructing a Citation through model_validate
    with an arbitrary artifact_kind string. Pydantic's Literal check will
    refuse most inputs, so we use a kind that's valid for engineering
    (e.g. 'pr') but invalid for clinical encounter contracts.
    """
    stub = _stub_enc_002()
    acc = "Diagnosis recorded with ICD-10 code in the chart."
    safety = "Allergy reconciliation completed before antibiotic order."
    contract = EncounterContract(
        external_id="ENC-002",
        acceptance_criteria=[acc],
        safety_criteria=[safety],
        citations={
            acc: [Citation(artifact_kind="pr", external_id="PR-002")],
            safety: [Citation(artifact_kind="order", external_id="ORD-002")],
        },
        drift_summary="Shipped as scoped.",
    )
    with pytest.raises(ValidationError):
        validate_encounter_contract(contract, stub)
