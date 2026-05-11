"""Tests for P2-4: LLM-backed clinical drafter path.

P2-4 mirrors P1-5 on the clinical side. The S2 stub registry
(``ENC-001..030``) keeps working untouched; encounters outside the
registry that previously raised ``NotImplementedError`` can now be
served by passing an ``LLMJudge`` instance to
``draft_encounter_contract``.

The judge is invoked with a prompt that instructs it to emit a
``JudgeOutput`` whose ``rationale`` field is a JSON string of the
``EncounterContract`` payload — we tunnel the structured output through
the existing ``JudgeOutput`` schema so the Merkle log, prompt_sha
registry, and record/replay store all continue to work unchanged.

These tests pre-populate a ``ReplayStore`` with hand-crafted recordings
or inject a ``MagicMock(spec=LLMJudge)``; nothing here reaches the
network.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from receipts.drafter import (
    EncounterContract,
    EncounterStub,
    ValidationError,
    draft_encounter_contract,
    validate_encounter_contract,
)
from receipts.drafter.llm_path import (
    ENCOUNTER_CONTRACT_PROMPT_TEMPLATE,
    draft_encounter_contract_llm,
)
from receipts.judge import JudgeCall, JudgeOutput, LLMJudge, ReplayStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_unknown() -> EncounterStub:
    """An encounter outside the stub registry (ENC-001..030)."""
    return EncounterStub(
        external_id="ENC-901",
        chief_complaint="Severe right-flank pain, 4-hour onset.",
        presenting_features=[
            "Colicky pain radiating to groin.",
            "Hematuria on urinalysis.",
            "No fever, no peritoneal signs.",
        ],
        audio_ref="s3://receipts-audio/enc-901.wav",
    )


def _stub_enc_001() -> EncounterStub:
    """A stub-registry encounter (ENC-001) — used to assert precedence."""
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


def _valid_encounter_contract_rationale() -> dict:
    """Build an EncounterContract payload (dict) consistent with ENC-901.

    Returned as a dict so callers can ``json.dumps(...)`` it into the
    ``rationale`` slot of a ``JudgeOutput`` payload.
    """
    acc = "Renal-colic workup documented with stone-protocol CT decision."
    safety = "Return precautions for fever / intractable pain documented."
    return {
        "external_id": "ENC-901",
        "acceptance_criteria": [acc],
        "safety_criteria": [safety],
        "citations": {
            acc: [
                {"artifact_kind": "note", "external_id": "NOTE-901", "locator": "mdm"},
                {"artifact_kind": "order", "external_id": "ORD-901-CT", "locator": None},
            ],
            safety: [
                {
                    "artifact_kind": "transcript",
                    "external_id": "TRX-901",
                    "locator": "00:14:22",
                },
            ],
        },
        "drift_summary": "Stone-protocol CT added after hematuria + colicky pattern.",
    }


def _judge_output_with_encounter_contract(rationale_payload: dict) -> str:
    """Return a JSON string parseable by ``JudgeOutput.model_validate_json``.

    The ``rationale`` field carries the encoded EncounterContract;
    ``score`` and ``flags`` are unused by the drafter path but must
    satisfy the schema.
    """
    payload = {
        "score": 1.0,
        "rationale": json.dumps(rationale_payload),
        "flags": [],
    }
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# LLM path: happy path
# ---------------------------------------------------------------------------


def test_llm_clinical_path_produces_valid_encounter_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay-mode happy path: recorded judge output parses into a valid contract.

    Pre-records a JudgeOutput whose rationale is a JSON EncounterContract
    payload, then asserts ``draft_encounter_contract_llm`` returns an
    ``EncounterContract`` that the validator accepts against the input
    stub.
    """
    monkeypatch.setenv("RECEIPTS_JUDGE_MODE", "replay")
    stub = _stub_unknown()
    store = ReplayStore(tmp_path)

    judge = LLMJudge(
        model="claude-opus-4-7",
        prompt_template=ENCOUNTER_CONTRACT_PROMPT_TEMPLATE,
        replay_store=store,
    )
    expected_payload = {"stub": stub.model_dump()}
    call = JudgeCall(
        model="claude-opus-4-7",
        prompt=ENCOUNTER_CONTRACT_PROMPT_TEMPLATE,
        temperature=0.0,
        seed=None,
        input_payload=expected_payload,
    )
    store.record(
        call,
        response={"text": _judge_output_with_encounter_contract(_valid_encounter_contract_rationale())},
        latency_ms=15,
        cost_usd=0.0,
    )

    contract = draft_encounter_contract_llm(stub, judge)
    assert isinstance(contract, EncounterContract)
    assert contract.external_id == "ENC-901"
    # Validator must accept the parsed output round-trip.
    validate_encounter_contract(contract, stub)
    # Safety floor survives the round-trip.
    assert len(contract.safety_criteria) >= 1
    assert "Stone-protocol CT" in contract.drift_summary


def test_llm_clinical_path_fails_on_missing_safety_criteria(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recording with empty safety_criteria must fail validator.

    The clinical safety floor is non-negotiable: even if the LLM emits a
    contract whose schema is otherwise valid, an empty safety_criteria
    list must surface as a ``ValidationError`` when passed through
    ``validate_encounter_contract``.
    """
    monkeypatch.setenv("RECEIPTS_JUDGE_MODE", "replay")
    stub = _stub_unknown()
    store = ReplayStore(tmp_path)

    judge = LLMJudge(
        model="claude-opus-4-7",
        prompt_template=ENCOUNTER_CONTRACT_PROMPT_TEMPLATE,
        replay_store=store,
    )
    acc = "Renal-colic workup documented with stone-protocol CT decision."
    no_safety = {
        "external_id": "ENC-901",
        "acceptance_criteria": [acc],
        "safety_criteria": [],  # missing safety — validator must reject.
        "citations": {
            acc: [
                {"artifact_kind": "note", "external_id": "NOTE-901", "locator": "mdm"},
            ],
        },
        "drift_summary": "Stone protocol CT added.",
    }
    call = JudgeCall(
        model="claude-opus-4-7",
        prompt=ENCOUNTER_CONTRACT_PROMPT_TEMPLATE,
        temperature=0.0,
        seed=None,
        input_payload={"stub": stub.model_dump()},
    )
    store.record(
        call,
        response={"text": _judge_output_with_encounter_contract(no_safety)},
        latency_ms=8,
        cost_usd=0.0,
    )

    contract = draft_encounter_contract_llm(stub, judge)
    # Drafter returns the contract as-is; downstream validator catches it.
    with pytest.raises(ValidationError):
        validate_encounter_contract(contract, stub)


# ---------------------------------------------------------------------------
# Dispatch: stub registry vs LLM path vs unknown
# ---------------------------------------------------------------------------


def test_stub_registry_still_takes_precedence_for_ENC_001() -> None:
    """ENC-001 returns the canned stub even when a judge is provided.

    The LLM path is opt-in *as a fallback*. For encounters in the stub
    registry, the deterministic canned output is the source of truth so
    fixture corpora stay byte-stable.
    """
    stub = _stub_enc_001()
    judge = MagicMock(spec=LLMJudge)

    contract = draft_encounter_contract(stub, judge=judge)

    # Stub was used; judge was never invoked.
    judge.evaluate.assert_not_called()
    assert contract.external_id == "ENC-001"
    # Canonical ENC-001 acceptance criterion includes "ACS workup".
    assert any("ACS workup" in c for c in contract.acceptance_criteria)


def test_unknown_enc_without_judge_raises_not_implemented() -> None:
    """Unchanged behavior: no judge + non-stub encounter still raises."""
    stub = _stub_unknown()
    with pytest.raises(NotImplementedError):
        draft_encounter_contract(stub)


def test_unknown_enc_with_judge_calls_llm_path() -> None:
    """Non-stub encounter + judge kwarg routes through the LLM path.

    Uses a MagicMock spec'd on ``LLMJudge`` so the test stays decoupled
    from replay-store wiring — the test only asserts that
    ``judge.evaluate`` is invoked with an input_payload that contains
    the stub.
    """
    stub = _stub_unknown()
    judge = MagicMock(spec=LLMJudge)
    judge.evaluate.return_value = JudgeOutput(
        score=1.0,
        rationale=json.dumps(_valid_encounter_contract_rationale()),
        flags=[],
    )

    contract = draft_encounter_contract(stub, judge=judge)

    judge.evaluate.assert_called_once()
    (input_payload,), _kwargs = judge.evaluate.call_args
    assert "stub" in input_payload
    assert input_payload["stub"]["external_id"] == "ENC-901"
    assert isinstance(contract, EncounterContract)
    validate_encounter_contract(contract, stub)


# ---------------------------------------------------------------------------
# Prompt template + export surface
# ---------------------------------------------------------------------------


def test_prompt_template_is_non_empty_string() -> None:
    """The exported prompt template must be present and informative.

    Downstream auditors recompute ``prompt_sha`` against this string;
    an empty / placeholder template would make the version registry
    meaningless.
    """
    assert isinstance(ENCOUNTER_CONTRACT_PROMPT_TEMPLATE, str)
    assert len(ENCOUNTER_CONTRACT_PROMPT_TEMPLATE) > 100
    # The template must mention the key schema fields so the LLM knows
    # what to emit; this keeps the prompt and the parser in sync.
    for token in (
        "acceptance_criteria",
        "safety_criteria",
        "citations",
        "drift_summary",
        "rationale",
        "transcript",
        "note",
        "order",
    ):
        assert token in ENCOUNTER_CONTRACT_PROMPT_TEMPLATE
