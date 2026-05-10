"""Revised-spec drafter + validator (S team).

This package compares an Epic's stated acceptance criteria against the
execution snapshot (PRs, meetings, threads) that materialized during the
sprint and emits a RevisedSpec — the spec rewritten to match what actually
shipped, with citations and a drift summary.

The drafter's LLM call is stubbed in S1; J4 will swap in the real model.
"""

from __future__ import annotations

from receipts.drafter.encounter_contract import draft_encounter_contract
from receipts.drafter.models import (
    Citation,
    EncounterContract,
    EncounterStub,
    Epic,
    Execution,
    MeetingRef,
    PRRef,
    RevisedSpec,
    ThreadRef,
)
from receipts.drafter.spec_drafter import draft_revised_spec
from receipts.drafter.validator import (
    ValidationError,
    validate_encounter_contract,
    validate_revised_spec,
)

__all__ = [
    "Citation",
    "EncounterContract",
    "EncounterStub",
    "Epic",
    "Execution",
    "MeetingRef",
    "PRRef",
    "RevisedSpec",
    "ThreadRef",
    "ValidationError",
    "draft_encounter_contract",
    "draft_revised_spec",
    "validate_encounter_contract",
    "validate_revised_spec",
]
