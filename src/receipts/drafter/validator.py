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

from receipts.drafter.models import ArtifactKind, Epic, Execution, RevisedSpec


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
