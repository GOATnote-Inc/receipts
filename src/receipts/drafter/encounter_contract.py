"""Clinical encounter-contract drafter — S2 stub.

S2 mirrors S1's contract surface for the clinical product. The drafter
accepts an EncounterStub and returns an EncounterContract keyed by
``stub.external_id`` against a small hand-crafted lookup (ENC-001..005).
J4 replaces this dispatch with a real LLM call.

Returning canned outputs forces downstream callers to handle the fact
that the drafter can rewrite criteria — drop, merge, rephrase — and not
merely echo presenting features. Crucially, the canned outputs always
include at least one safety_criterion: clinical encounters have a
non-optional safety floor and the contract surface reflects that.
"""

from __future__ import annotations

from collections.abc import Callable

from receipts.drafter.models import Citation, EncounterContract, EncounterStub


def _draft_enc_001(_stub: EncounterStub) -> EncounterContract:
    """ENC-001 — chest pain workup, drift: patient declined troponin recheck."""
    acc = "ACS workup documented with risk stratification (HEART score)."
    acc_2 = "Initial troponin and ECG completed within 30 minutes of arrival."
    safety = "Repeat troponin recheck refusal documented with patient capacity attested."
    return EncounterContract(
        external_id="ENC-001",
        acceptance_criteria=[acc, acc_2],
        safety_criteria=[safety],
        citations={
            acc: [
                Citation(artifact_kind="note", external_id="NOTE-001", locator="hpi"),
                Citation(artifact_kind="transcript", external_id="TRX-001", locator="00:04:12"),
            ],
            acc_2: [
                Citation(artifact_kind="order", external_id="ORD-001-TROP"),
                Citation(artifact_kind="order", external_id="ORD-001-ECG"),
            ],
            safety: [
                Citation(artifact_kind="transcript", external_id="TRX-001", locator="00:11:48"),
                Citation(artifact_kind="note", external_id="NOTE-001", locator="mdm"),
            ],
        },
        drift_summary=(
            "Planned 3-hour repeat troponin declined by patient against medical "
            "advice; documented capacity and refusal rather than escalating to "
            "admission. Original presenting picture (chest pain, diaphoresis, "
            "low initial troponin) did not change."
        ),
    )


def _draft_enc_002(_stub: EncounterStub) -> EncounterContract:
    """ENC-002 — uncomplicated UTI, no drift."""
    acc = "Uncomplicated cystitis diagnosis recorded with ICD-10 code."
    safety = "Allergy reconciliation completed before antibiotic order."
    return EncounterContract(
        external_id="ENC-002",
        acceptance_criteria=[acc],
        safety_criteria=[safety],
        citations={
            acc: [
                Citation(artifact_kind="note", external_id="NOTE-002", locator="assessment"),
            ],
            safety: [
                Citation(artifact_kind="order", external_id="ORD-002-ABX"),
                Citation(artifact_kind="note", external_id="NOTE-002", locator="allergies"),
            ],
        },
        drift_summary="Shipped as scoped — no drift.",
    )


def _draft_enc_003(_stub: EncounterStub) -> EncounterContract:
    """ENC-003 — pediatric fever, workup expanded after exam findings."""
    acc = "Source-of-fever workup documented with age-appropriate differential."
    acc_2 = "Disposition decision linked to revised Yale Observation Score."
    safety = "Caregiver return precautions documented and read-back confirmed."
    return EncounterContract(
        external_id="ENC-003",
        acceptance_criteria=[acc, acc_2],
        safety_criteria=[safety],
        citations={
            acc: [
                Citation(artifact_kind="note", external_id="NOTE-003", locator="ros"),
                Citation(artifact_kind="order", external_id="ORD-003-CBC"),
            ],
            acc_2: [
                Citation(artifact_kind="note", external_id="NOTE-003", locator="mdm"),
            ],
            safety: [
                Citation(artifact_kind="transcript", external_id="TRX-003", locator="00:18:02"),
            ],
        },
        drift_summary=(
            "Workup expanded mid-encounter after exam revealed tachypnea; "
            "added CXR + UA beyond initial CBC-only plan documented in HPI."
        ),
    )


def _draft_enc_004(_stub: EncounterStub) -> EncounterContract:
    """ENC-004 — laceration repair, scope rephrased for tetanus status."""
    acc = "Wound cleaned, anesthetized, and closed with sutures appropriate to depth."
    safety = "Tetanus immunization status verified and updated as indicated."
    return EncounterContract(
        external_id="ENC-004",
        acceptance_criteria=[acc],
        safety_criteria=[safety],
        citations={
            acc: [
                Citation(artifact_kind="note", external_id="NOTE-004", locator="procedure"),
            ],
            safety: [
                Citation(artifact_kind="order", external_id="ORD-004-TDAP"),
                Citation(artifact_kind="note", external_id="NOTE-004", locator="immunizations"),
            ],
        },
        drift_summary=(
            "Tetanus status criterion rephrased from 'reviewed' to explicit "
            "'verified and updated as indicated' after Tdap was actually given."
        ),
    )


def _draft_enc_005(_stub: EncounterStub) -> EncounterContract:
    """ENC-005 — psych eval, additional safety criterion added."""
    acc = "Psychiatric evaluation documented with risk assessment."
    safety = "Suicide risk assessed with structured tool and disposition justified."
    safety_2 = "Means restriction counseling documented when risk is non-low."
    return EncounterContract(
        external_id="ENC-005",
        acceptance_criteria=[acc],
        safety_criteria=[safety, safety_2],
        citations={
            acc: [
                Citation(artifact_kind="note", external_id="NOTE-005", locator="psych"),
            ],
            safety: [
                Citation(artifact_kind="note", external_id="NOTE-005", locator="risk"),
                Citation(artifact_kind="transcript", external_id="TRX-005", locator="00:22:30"),
            ],
            safety_2: [
                Citation(artifact_kind="transcript", external_id="TRX-005", locator="00:31:15"),
            ],
        },
        drift_summary=(
            "Means-restriction safety criterion added after risk stratified to "
            "moderate; original stub anticipated only a single safety criterion."
        ),
    )


_STUB_REGISTRY: dict[str, Callable[[EncounterStub], EncounterContract]] = {
    "ENC-001": _draft_enc_001,
    "ENC-002": _draft_enc_002,
    "ENC-003": _draft_enc_003,
    "ENC-004": _draft_enc_004,
    "ENC-005": _draft_enc_005,
}


def draft_encounter_contract(stub: EncounterStub) -> EncounterContract:
    """Return a canned EncounterContract for known stub external_ids.

    Raises NotImplementedError for any encounter outside the stub registry —
    J4 will replace this with a real LLM dispatch.
    """
    try:
        builder = _STUB_REGISTRY[stub.external_id]
    except KeyError as exc:
        raise NotImplementedError(
            f"S2 stub has no canned EncounterContract for stub external_id="
            f"{stub.external_id!r}; real LLM dispatch is J4's responsibility."
        ) from exc
    return builder(stub)
