"""Validator for RevisedSpec outputs.

Enforces three contracts:

(a) every criterion in ``spec.acceptance_criteria`` has at least one Citation
    that resolves to an artifact present in the input ``Execution``;
(b) no Citation references an (artifact_kind, external_id) pair that wasn't
    in the Execution — no phantom entities;
(c) the RevisedSpec itself is a valid pydantic v2 model (re-validated here
    so callers that constructed it loosely still get the check).
"""

from __future__ import annotations

from pydantic import ValidationError as PydanticValidationError

from receipts.drafter.models import (
    ENCOUNTER_ARTIFACT_KINDS,
    ArtifactKind,
    EncounterContract,
    EncounterStub,
    Epic,
    Execution,
    RevisedSpec,
)


class ValidationError(Exception):
    """Raised when a RevisedSpec fails one of the S1 validator contracts."""


def _execution_index(execution: Execution) -> dict[ArtifactKind, set[str]]:
    """Group available artifact external_ids by kind for fast membership tests."""
    return {
        "pr": {ref.external_id for ref in execution.prs},
        "meeting": {ref.external_id for ref in execution.meetings},
        "thread": {ref.external_id for ref in execution.threads},
    }


def validate_revised_spec(
    spec: RevisedSpec,
    epic: Epic,
    execution: Execution,
) -> None:
    """Validate a RevisedSpec against its source Epic + Execution.

    Returns None on success; raises ``ValidationError`` otherwise.

    The ``epic`` argument is accepted for the contract surface — J4's real
    drafter will use it for additional invariants (e.g. drift summary must
    not invent criteria that have no counterpart in either epic or
    execution). S1 keeps the check minimal but keeps the argument so the
    signature is stable.
    """
    # (c) pydantic schema valid — re-run model_validate to catch loose construction.
    try:
        RevisedSpec.model_validate(spec.model_dump())
    except PydanticValidationError as exc:
        raise ValidationError(f"RevisedSpec failed pydantic schema validation: {exc}") from exc

    if not isinstance(epic, Epic):  # defensive — callers may pass dicts.
        raise ValidationError("validate_revised_spec requires an Epic instance.")

    index = _execution_index(execution)

    # (a) every criterion has ≥1 citation.
    for criterion in spec.acceptance_criteria:
        citations = spec.citations.get(criterion, [])
        if not citations:
            raise ValidationError(
                f"Criterion has no citations: {criterion!r}. "
                "Every revised criterion must cite at least one execution artifact."
            )
        # (b) each citation must resolve into the execution.
        for citation in citations:
            available = index.get(citation.artifact_kind, set())
            if citation.external_id not in available:
                raise ValidationError(
                    f"Citation references unknown artifact "
                    f"({citation.artifact_kind}={citation.external_id!r}) "
                    f"for criterion {criterion!r}; not present in Execution."
                )

    # (b, cont.) flag citations whose key is not even in the criteria list.
    stray = [key for key in spec.citations if key not in spec.acceptance_criteria]
    if stray:
        raise ValidationError(
            f"Citations reference criteria not in acceptance_criteria: {stray!r}."
        )


# ---------------------------------------------------------------------------
# Clinical encounter-contract validator (S2)
# ---------------------------------------------------------------------------


def validate_encounter_contract(
    contract: EncounterContract,
    stub: EncounterStub,
) -> None:
    """Validate an EncounterContract against its source EncounterStub.

    Returns None on success; raises ``ValidationError`` otherwise.

    Contracts enforced:
      (a) every criterion (acceptance OR safety) has ≥1 citation;
      (b) ``safety_criteria`` is non-empty — clinical encounters require a
          defined safety floor;
      (c) every citation references an artifact_kind in
          {"transcript", "note", "order"} — engineering kinds (pr/meeting/
          thread) are rejected on a clinical citation;
      (d) the EncounterContract is a valid pydantic v2 model.

    The ``stub`` argument is accepted for the contract surface so J4's real
    drafter can layer in stub-coherence checks (e.g. drift summary must
    reference the stub's chief complaint or presenting features). S2 keeps
    the check minimal but keeps the argument so the signature is stable.
    """
    # (d) pydantic schema valid — re-run model_validate to catch loose construction.
    try:
        EncounterContract.model_validate(contract.model_dump())
    except PydanticValidationError as exc:
        raise ValidationError(
            f"EncounterContract failed pydantic schema validation: {exc}"
        ) from exc

    if not isinstance(stub, EncounterStub):  # defensive — callers may pass dicts.
        raise ValidationError("validate_encounter_contract requires an EncounterStub instance.")

    # (b) safety floor: at least one safety criterion.
    if not contract.safety_criteria:
        raise ValidationError(
            "EncounterContract.safety_criteria is empty; clinical encounters "
            "require at least one safety criterion."
        )

    all_criteria = list(contract.acceptance_criteria) + list(contract.safety_criteria)

    # (a) every criterion has ≥1 citation.
    # (c) every citation references an allowed clinical artifact_kind.
    for criterion in all_criteria:
        citations = contract.citations.get(criterion, [])
        if not citations:
            raise ValidationError(
                f"Criterion has no citations: {criterion!r}. Every encounter "
                "criterion (acceptance or safety) must cite at least one "
                "transcript / note / order artifact."
            )
        for citation in citations:
            if citation.artifact_kind not in ENCOUNTER_ARTIFACT_KINDS:
                raise ValidationError(
                    f"Citation for criterion {criterion!r} references disallowed "
                    f"artifact_kind={citation.artifact_kind!r}; encounter contracts "
                    f"may cite only {sorted(ENCOUNTER_ARTIFACT_KINDS)!r}."
                )

    # Flag citations whose key is not in the contract's criteria at all.
    stray = [key for key in contract.citations if key not in all_criteria]
    if stray:
        raise ValidationError(f"Citations reference criteria not in the contract: {stray!r}.")
