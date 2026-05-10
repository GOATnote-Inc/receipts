"""Tests for the revised-spec drafter + validator (S1).

The drafter consumes an Epic (the original intent) plus an Execution snapshot
(PRs/meetings/threads that actually happened) and emits a RevisedSpec — the
spec rewritten to match what shipped, with citations back to source artifacts
and a drift summary. The validator enforces three contracts:

  (a) every criterion is cited by at least one Citation whose external_id
      points to an artifact present in the Execution.
  (b) no Citation references an artifact_kind/external_id that wasn't in the
      input Execution (no phantom entities).
  (c) the RevisedSpec is a valid pydantic v2 model.

The LLM is stubbed: draft_revised_spec dispatches on epic.external_id against
a small hand-written lookup. Real LLM wiring is J4's territory.
"""

from __future__ import annotations

import pytest

from receipts.drafter import (
    Citation,
    Epic,
    Execution,
    MeetingRef,
    PRRef,
    RevisedSpec,
    ThreadRef,
    ValidationError,
    draft_revised_spec,
    validate_revised_spec,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _execution_001() -> Execution:
    """Execution context for EPIC-001 (drift case)."""
    return Execution(
        prs=[
            PRRef(
                external_id="PR-101",
                repo="receipts",
                number=101,
                diff_summary="Add /v1/spec endpoint with epic_id query param.",
            ),
            PRRef(
                external_id="PR-102",
                repo="receipts",
                number=102,
                diff_summary="Wire spec endpoint into CLI; add fixture corpus.",
            ),
        ],
        meetings=[
            MeetingRef(
                external_id="MTG-21",
                decisions=[
                    "Defer batch endpoint to next sprint.",
                    "Single-epic lookup is acceptance scope for v1.",
                ],
            ),
        ],
        threads=[
            ThreadRef(
                external_id="THR-7",
                channel="#receipts",
                summary="Confirmed CLI consumes the new endpoint.",
            ),
        ],
    )


def _execution_002() -> Execution:
    """Execution context for EPIC-002 (no-drift case)."""
    return Execution(
        prs=[
            PRRef(
                external_id="PR-201",
                repo="receipts",
                number=201,
                diff_summary="Implement Merkle log append with SHA-256 chain.",
            ),
        ],
        meetings=[
            MeetingRef(
                external_id="MTG-30",
                decisions=["Ship as scoped; no follow-ups."],
            ),
        ],
        threads=[],
    )


def _epic_001() -> Epic:
    return Epic(
        id=1,
        external_id="EPIC-001",
        title="Expose revised-spec endpoint",
        acceptance_criteria=[
            "GET /v1/spec returns the latest revised spec for a given epic_id.",
            "Batch lookup for many epic_ids in a single request.",
        ],
    )


def _epic_002() -> Epic:
    return Epic(
        id=2,
        external_id="EPIC-002",
        title="Merkle ledger append",
        acceptance_criteria=[
            "Appending an event extends the SHA-256 chain.",
        ],
    )


# ---------------------------------------------------------------------------
# Drafter tests
# ---------------------------------------------------------------------------


def test_draft_returns_revised_spec_for_drift_case() -> None:
    """EPIC-001 dropped batch lookup; drift_summary must mention it."""
    epic = _epic_001()
    execution = _execution_001()

    spec = draft_revised_spec(epic, execution)

    assert isinstance(spec, RevisedSpec)
    assert len(spec.acceptance_criteria) >= 1
    assert spec.drift_summary  # non-empty
    assert "batch" in spec.drift_summary.lower()
    # Every emitted criterion must have at least one citation.
    for criterion in spec.acceptance_criteria:
        assert criterion in spec.citations
        assert len(spec.citations[criterion]) >= 1
    # Validator must accept the drafter's own output.
    validate_revised_spec(spec, epic, execution)


def test_draft_returns_revised_spec_for_no_drift() -> None:
    """EPIC-002 shipped as scoped; drift_summary must say so."""
    epic = _epic_002()
    execution = _execution_002()

    spec = draft_revised_spec(epic, execution)

    assert isinstance(spec, RevisedSpec)
    assert len(spec.acceptance_criteria) == 1
    assert spec.drift_summary
    lowered = spec.drift_summary.lower()
    assert ("no drift" in lowered) or ("as scoped" in lowered) or ("no change" in lowered)
    validate_revised_spec(spec, epic, execution)


def test_draft_unknown_epic_raises_not_implemented() -> None:
    """Stub returns NotImplementedError for epics outside the fixture lookup."""
    epic = Epic(
        id=99,
        external_id="EPIC-UNKNOWN",
        title="not in lookup",
        acceptance_criteria=["something"],
    )
    execution = Execution(prs=[], meetings=[], threads=[])
    with pytest.raises(NotImplementedError):
        draft_revised_spec(epic, execution)


# ---------------------------------------------------------------------------
# Validator tests
# ---------------------------------------------------------------------------


def test_validator_passes_on_well_cited() -> None:
    """Hand-crafted RevisedSpec with valid citations passes."""
    epic = _epic_002()
    execution = _execution_002()
    criterion = "Appending an event extends the SHA-256 chain."
    spec = RevisedSpec(
        acceptance_criteria=[criterion],
        citations={
            criterion: [
                Citation(artifact_kind="pr", external_id="PR-201", locator=None),
                Citation(artifact_kind="meeting", external_id="MTG-30", locator="0"),
            ],
        },
        drift_summary="Shipped as scoped — no drift.",
    )
    # Returns None on success.
    assert validate_revised_spec(spec, epic, execution) is None


def test_validator_fails_on_missing_citation() -> None:
    """A criterion without any citation must trip ValidationError."""
    epic = _epic_002()
    execution = _execution_002()
    criterion = "Appending an event extends the SHA-256 chain."
    spec = RevisedSpec(
        acceptance_criteria=[criterion],
        citations={},  # no citation at all
        drift_summary="Shipped as scoped.",
    )
    with pytest.raises(ValidationError):
        validate_revised_spec(spec, epic, execution)


def test_validator_fails_on_empty_citation_list() -> None:
    """Citation key present but with an empty list — still no support."""
    epic = _epic_002()
    execution = _execution_002()
    criterion = "Appending an event extends the SHA-256 chain."
    spec = RevisedSpec(
        acceptance_criteria=[criterion],
        citations={criterion: []},
        drift_summary="Shipped as scoped.",
    )
    with pytest.raises(ValidationError):
        validate_revised_spec(spec, epic, execution)


def test_validator_fails_on_phantom_entity() -> None:
    """Citation references an external_id that was never in the Execution."""
    epic = _epic_002()
    execution = _execution_002()
    criterion = "Appending an event extends the SHA-256 chain."
    spec = RevisedSpec(
        acceptance_criteria=[criterion],
        citations={
            criterion: [
                Citation(artifact_kind="pr", external_id="PR-DOES-NOT-EXIST"),
            ],
        },
        drift_summary="Shipped as scoped.",
    )
    with pytest.raises(ValidationError):
        validate_revised_spec(spec, epic, execution)


def test_validator_fails_on_wrong_artifact_kind() -> None:
    """Citation external_id exists in Execution but under a different kind."""
    epic = _epic_002()
    execution = _execution_002()
    criterion = "Appending an event extends the SHA-256 chain."
    # PR-201 is a PR, not a meeting.
    spec = RevisedSpec(
        acceptance_criteria=[criterion],
        citations={
            criterion: [Citation(artifact_kind="meeting", external_id="PR-201")],
        },
        drift_summary="Shipped as scoped.",
    )
    with pytest.raises(ValidationError):
        validate_revised_spec(spec, epic, execution)
