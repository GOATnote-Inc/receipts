"""Revised-spec drafter — S1 stub.

S1 is the contract surface. The drafter accepts an Epic + Execution and
returns a RevisedSpec keyed by ``epic.external_id`` against a small
hand-crafted lookup. J4 replaces this dispatch with a real LLM call.

Returning canned outputs (rather than mechanically synthesizing one from
the inputs) is intentional: it forces downstream callers to handle the
fact that the drafter can *re-write* criteria — drop, merge, rephrase —
and not merely echo them.
"""

from __future__ import annotations

from collections.abc import Callable

from receipts.drafter.models import Citation, Epic, Execution, RevisedSpec


def _draft_epic_001(_execution: Execution) -> RevisedSpec:
    """EPIC-001 — batch endpoint was dropped mid-sprint."""
    criterion = "GET /v1/spec returns the latest revised spec for a given epic_id."
    return RevisedSpec(
        acceptance_criteria=[criterion],
        citations={
            criterion: [
                Citation(artifact_kind="pr", external_id="PR-101", locator=None),
                Citation(artifact_kind="pr", external_id="PR-102", locator=None),
                Citation(artifact_kind="meeting", external_id="MTG-21", locator="1"),
                Citation(artifact_kind="thread", external_id="THR-7", locator=None),
            ],
        },
        drift_summary=(
            "Batch lookup criterion was dropped in MTG-21 to keep v1 scope to "
            "single-epic GET. Endpoint shipped via PR-101/PR-102."
        ),
    )


def _draft_epic_002(_execution: Execution) -> RevisedSpec:
    """EPIC-002 — shipped as scoped, no drift."""
    criterion = "Appending an event extends the SHA-256 chain."
    return RevisedSpec(
        acceptance_criteria=[criterion],
        citations={
            criterion: [
                Citation(artifact_kind="pr", external_id="PR-201", locator=None),
                Citation(artifact_kind="meeting", external_id="MTG-30", locator="0"),
            ],
        },
        drift_summary="Shipped as scoped — no drift.",
    )


def _draft_epic_003(_execution: Execution) -> RevisedSpec:
    """EPIC-003 — judge L0 patterns, one criterion split into two."""
    cri_a = "L0 scorer flags missing-citation patterns in <50ms per criterion."
    cri_b = "L0 scorer emits a structured failure-class label per finding."
    return RevisedSpec(
        acceptance_criteria=[cri_a, cri_b],
        citations={
            cri_a: [Citation(artifact_kind="pr", external_id="PR-301")],
            cri_b: [
                Citation(artifact_kind="pr", external_id="PR-302"),
                Citation(artifact_kind="thread", external_id="THR-12"),
            ],
        },
        drift_summary=(
            "Original single criterion split into latency + label criteria "
            "after THR-12 surfaced ambiguity in 'flags' wording."
        ),
    )


def _draft_epic_004(_execution: Execution) -> RevisedSpec:
    """EPIC-004 — connector shim, criterion rephrased."""
    criterion = "Linear connector replays a fixture issue and round-trips fields."
    return RevisedSpec(
        acceptance_criteria=[criterion],
        citations={
            criterion: [
                Citation(artifact_kind="pr", external_id="PR-401"),
                Citation(artifact_kind="meeting", external_id="MTG-44", locator="2"),
            ],
        },
        drift_summary=(
            "Criterion rephrased from 'reads issues' to explicit fixture-replay "
            "+ round-trip per MTG-44 decision #2."
        ),
    )


def _draft_epic_005(_execution: Execution) -> RevisedSpec:
    """EPIC-005 — meeting transcript ingest, scope expanded."""
    cri_a = "Transcript ingest writes a meeting row with decision array."
    cri_b = "Transcript ingest deduplicates by transcript external_id."
    return RevisedSpec(
        acceptance_criteria=[cri_a, cri_b],
        citations={
            cri_a: [Citation(artifact_kind="pr", external_id="PR-501")],
            cri_b: [
                Citation(artifact_kind="pr", external_id="PR-502"),
                Citation(artifact_kind="meeting", external_id="MTG-55", locator="0"),
            ],
        },
        drift_summary=(
            "Dedup criterion added after MTG-55 reviewed accidental double-writes "
            "in the synthetic week fixture."
        ),
    )


_STUB_REGISTRY: dict[str, Callable[[Execution], RevisedSpec]] = {
    "EPIC-001": _draft_epic_001,
    "EPIC-002": _draft_epic_002,
    "EPIC-003": _draft_epic_003,
    "EPIC-004": _draft_epic_004,
    "EPIC-005": _draft_epic_005,
}


def draft_revised_spec(epic: Epic, execution: Execution) -> RevisedSpec:
    """Return a canned RevisedSpec for known epic external_ids.

    Raises NotImplementedError for any epic outside the stub registry —
    J4 will replace this with a real LLM dispatch.
    """
    try:
        builder = _STUB_REGISTRY[epic.external_id]
    except KeyError as exc:
        raise NotImplementedError(
            f"S1 stub has no canned RevisedSpec for epic external_id={epic.external_id!r}; "
            "real LLM dispatch is J4's responsibility."
        ) from exc
    return builder(execution)
