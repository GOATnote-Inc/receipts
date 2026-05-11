"""Tests for P1-5: real-LLM drafter path.

P1-5 wires a real LLM call (via ``LLMJudge`` + ``ReplayStore``) into the
revised-spec drafter as an opt-in branch. The S1 stub registry
(``EPIC-001..030``) keeps working untouched; epics outside the registry
that previously raised ``NotImplementedError`` can now be served by
passing an ``LLMJudge`` instance.

The judge is invoked with a prompt that instructs it to emit a
``JudgeOutput`` whose ``rationale`` field is a JSON string of the
``RevisedSpec`` payload — we tunnel the structured output through the
existing ``JudgeOutput`` schema so the Merkle log, prompt_sha registry,
and record/replay store all continue to work with no schema changes.

These tests pre-populate a ``ReplayStore`` with hand-crafted recordings
or inject a mocked ``LLMJudge``; nothing here reaches the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from receipts.drafter import (
    Citation,
    Epic,
    Execution,
    MeetingRef,
    PRRef,
    RevisedSpec,
    ThreadRef,
    draft_revised_spec,
    validate_revised_spec,
)
from receipts.drafter.llm_path import (
    REVISED_SPEC_PROMPT_TEMPLATE,
    draft_revised_spec_llm,
)
from receipts.judge import JudgeCall, JudgeOutput, LLMJudge, ReplayStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _execution_unknown() -> Execution:
    """Execution context for a non-stub epic."""
    return Execution(
        prs=[
            PRRef(
                external_id="PR-901",
                repo="receipts",
                number=901,
                diff_summary="Add feature foo.",
            ),
        ],
        meetings=[
            MeetingRef(
                external_id="MTG-90",
                decisions=["Ship as scoped."],
            ),
        ],
        threads=[
            ThreadRef(
                external_id="THR-90",
                channel="#receipts",
                summary="Discussion of feature foo.",
            ),
        ],
    )


def _epic_unknown() -> Epic:
    """An epic outside the stub registry (EPIC-001..030)."""
    return Epic(
        id=901,
        external_id="EPIC-901",
        title="Feature foo",
        acceptance_criteria=[
            "Foo endpoint returns shaped JSON.",
        ],
    )


def _epic_001() -> Epic:
    """A stub-registry epic (EPIC-001) — used to assert precedence."""
    return Epic(
        id=1,
        external_id="EPIC-001",
        title="Expose revised-spec endpoint",
        acceptance_criteria=[
            "GET /v1/spec returns the latest revised spec for a given epic_id.",
        ],
    )


def _valid_revised_spec_rationale(execution: Execution) -> dict:
    """Build a RevisedSpec payload (dict) consistent with the execution.

    Returned as a dict so callers can ``json.dumps(...)`` it into the
    ``rationale`` slot of a ``JudgeOutput`` payload.
    """
    criterion = "Foo endpoint returns shaped JSON consistent with PR-901."
    return {
        "acceptance_criteria": [criterion],
        "citations": {
            criterion: [
                {"artifact_kind": "pr", "external_id": "PR-901", "locator": None},
                {"artifact_kind": "meeting", "external_id": "MTG-90", "locator": "0"},
            ],
        },
        "drift_summary": "Shipped as scoped per MTG-90.",
    }


def _judge_output_with_revised_spec(execution: Execution) -> str:
    """Return a JSON string parseable by ``JudgeOutput.model_validate_json``.

    The ``rationale`` field carries the encoded RevisedSpec; ``score`` and
    ``flags`` are unused by the drafter path but must satisfy the schema.
    """
    payload = {
        "score": 1.0,
        "rationale": json.dumps(_valid_revised_spec_rationale(execution)),
        "flags": [],
    }
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# LLM path: happy path
# ---------------------------------------------------------------------------


def test_llm_path_produces_valid_revised_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay-mode happy path: a recorded judge output parses into a valid spec.

    Pre-records a JudgeOutput whose rationale is a JSON RevisedSpec, then
    asserts ``draft_revised_spec_llm`` returns a ``RevisedSpec`` that the
    validator accepts against the input epic + execution.
    """
    monkeypatch.setenv("RECEIPTS_JUDGE_MODE", "replay")
    epic = _epic_unknown()
    execution = _execution_unknown()
    store = ReplayStore(tmp_path)

    # Pre-record the call: the input_payload shape is whatever the
    # drafter builds internally — we mirror it here so the replay hits.
    judge = LLMJudge(
        model="claude-opus-4-7",
        prompt_template=REVISED_SPEC_PROMPT_TEMPLATE,
        replay_store=store,
    )
    expected_payload = {
        "epic": epic.model_dump(),
        "execution": execution.model_dump(),
    }
    call = JudgeCall(
        model="claude-opus-4-7",
        prompt=REVISED_SPEC_PROMPT_TEMPLATE,
        temperature=0.0,
        seed=None,
        input_payload=expected_payload,
    )
    store.record(
        call,
        response={"text": _judge_output_with_revised_spec(execution)},
        latency_ms=12,
        cost_usd=0.0,
    )

    spec = draft_revised_spec_llm(epic, execution, judge)
    assert isinstance(spec, RevisedSpec)
    # The validator must accept the parsed output round-trip.
    validate_revised_spec(spec, epic, execution)
    # Drift summary survives the round-trip.
    assert "MTG-90" in spec.drift_summary


def test_llm_path_falls_back_when_rationale_not_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-JSON rationale must raise ``ValueError`` from the drafter.

    The LLM is contracted to emit a JSON-encoded RevisedSpec inside
    ``rationale``. If it emits a free-form string instead, the drafter
    refuses rather than silently producing an empty spec — the caller
    can decide whether to retry / surface to a human.
    """
    monkeypatch.setenv("RECEIPTS_JUDGE_MODE", "replay")
    epic = _epic_unknown()
    execution = _execution_unknown()
    store = ReplayStore(tmp_path)

    judge = LLMJudge(
        model="claude-opus-4-7",
        prompt_template=REVISED_SPEC_PROMPT_TEMPLATE,
        replay_store=store,
    )
    bad_payload = json.dumps(
        {
            "score": 0.5,
            "rationale": "this is plain prose, not JSON at all",
            "flags": [],
        }
    )
    call = JudgeCall(
        model="claude-opus-4-7",
        prompt=REVISED_SPEC_PROMPT_TEMPLATE,
        temperature=0.0,
        seed=None,
        input_payload={
            "epic": epic.model_dump(),
            "execution": execution.model_dump(),
        },
    )
    store.record(call, response={"text": bad_payload}, latency_ms=5, cost_usd=0.0)

    with pytest.raises(ValueError, match="rationale"):
        draft_revised_spec_llm(epic, execution, judge)


# ---------------------------------------------------------------------------
# Dispatch: stub registry vs LLM path vs unknown
# ---------------------------------------------------------------------------


def test_stub_registry_still_takes_precedence(tmp_path: Path) -> None:
    """EPIC-001 returns the canned stub even when a judge is provided.

    The LLM path is opt-in *as a fallback*. For epics in the stub
    registry, the deterministic canned output is the source of truth so
    fixture corpora stay byte-stable.
    """
    epic = _epic_001()
    execution = Execution(
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
                diff_summary="Wire spec endpoint into CLI.",
            ),
        ],
        meetings=[
            MeetingRef(
                external_id="MTG-21",
                decisions=["Defer batch endpoint."],
            ),
        ],
        threads=[
            ThreadRef(external_id="THR-7", channel="#receipts", summary="Confirmed CLI."),
        ],
    )
    judge = MagicMock(spec=LLMJudge)

    spec = draft_revised_spec(epic, execution, judge=judge)

    # Stub was used; judge was never invoked.
    judge.evaluate.assert_not_called()
    # Canonical stub criterion text is the marker.
    assert any(
        "GET /v1/spec" in criterion for criterion in spec.acceptance_criteria
    )


def test_unknown_epic_without_judge_raises_not_implemented() -> None:
    """Unchanged behavior: no judge + non-stub epic still raises."""
    epic = _epic_unknown()
    execution = _execution_unknown()
    with pytest.raises(NotImplementedError):
        draft_revised_spec(epic, execution)


def test_unknown_epic_with_judge_calls_llm_path() -> None:
    """Non-stub epic + judge kwarg routes through the LLM path.

    Uses a MagicMock spec'd on ``LLMJudge`` so the test stays decoupled
    from replay-store wiring — the test only asserts that
    ``judge.evaluate`` is invoked with an input_payload that contains
    both the epic and the execution.
    """
    epic = _epic_unknown()
    execution = _execution_unknown()

    judge = MagicMock(spec=LLMJudge)
    judge.evaluate.return_value = JudgeOutput(
        score=1.0,
        rationale=json.dumps(_valid_revised_spec_rationale(execution)),
        flags=[],
    )

    spec = draft_revised_spec(epic, execution, judge=judge)

    judge.evaluate.assert_called_once()
    (input_payload,), _kwargs = judge.evaluate.call_args
    assert "epic" in input_payload
    assert "execution" in input_payload
    assert input_payload["epic"]["external_id"] == "EPIC-901"
    assert isinstance(spec, RevisedSpec)
    validate_revised_spec(spec, epic, execution)


# ---------------------------------------------------------------------------
# Prompt template + export surface
# ---------------------------------------------------------------------------


def test_prompt_template_is_non_empty_string() -> None:
    """The exported prompt template must be present and informative.

    Downstream auditors recompute ``prompt_sha`` against this string;
    an empty / placeholder template would make the version registry
    meaningless.
    """
    assert isinstance(REVISED_SPEC_PROMPT_TEMPLATE, str)
    assert len(REVISED_SPEC_PROMPT_TEMPLATE) > 100
    # The template must mention the key schema fields so the LLM knows
    # what to emit; this keeps the prompt and the parser in sync.
    for token in ("acceptance_criteria", "citations", "drift_summary", "rationale"):
        assert token in REVISED_SPEC_PROMPT_TEMPLATE


def test_llm_path_citation_with_unknown_artifact_fails_validator(
    tmp_path: Path,
) -> None:
    """A judge-emitted phantom citation must surface as a validator error.

    The drafter does not silently filter LLM outputs — if the model
    invents a PR id, the existing validator should flag it once the
    drafter passes the parsed spec through.
    """
    epic = _epic_unknown()
    execution = _execution_unknown()
    judge = MagicMock(spec=LLMJudge)

    bad_rationale = {
        "acceptance_criteria": ["Foo endpoint returns shaped JSON."],
        "citations": {
            "Foo endpoint returns shaped JSON.": [
                {"artifact_kind": "pr", "external_id": "PR-DOES-NOT-EXIST"},
            ],
        },
        "drift_summary": "Phantom citation case.",
    }
    judge.evaluate.return_value = JudgeOutput(
        score=1.0,
        rationale=json.dumps(bad_rationale),
        flags=[],
    )

    spec = draft_revised_spec_llm(epic, execution, judge)
    # Drafter returns the spec as-is; downstream validator surfaces the
    # phantom citation.
    from receipts.drafter import ValidationError

    with pytest.raises(ValidationError):
        validate_revised_spec(spec, epic, execution)
