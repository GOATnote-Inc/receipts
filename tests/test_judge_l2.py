"""Tests for J4: L2 LLM judge wrapper + version registry.

This wrapper is the thin shim between the rest of the receipts pipeline and
the live Anthropic / OpenAI SDKs. Three contracts matter, and each has a
dedicated test below:

1. **Hermetic by default.** ``ReplayStore.mode_from_env`` defaults to
   ``"replay"`` so ``make test`` never reaches the network. All tests below
   pre-populate a ``ReplayStore`` or mock the SDK client; nothing here
   touches the wire.
2. **Version registry via prompt_sha.** Every judge invocation stamps the
   sha256 of the prompt template into the Merkle log so later audits can
   correlate a verdict with the exact prompt that produced it.
3. **Model-specific kwargs are enforced.** ``claude-opus-4-7`` rejects
   ``temperature``/``top_p``/``top_k``/``budget_tokens``; ``gpt-5.4-*``
   requires ``max_completion_tokens`` (not ``max_tokens``). Adapters honour
   these constraints; the tests pin the wire-level kwargs.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from receipts.judge import (
    AnthropicAdapter,
    JudgeCall,
    JudgeOutput,
    JudgeRecording,
    LLMJudge,
    OpenAIAdapter,
    ReplayStore,
)
from receipts.ledger.merkle import MerkleLog
from receipts.ledger.models import Attestation

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


# --------------------------- helpers / fixtures ---------------------------


PROMPT_TEMPLATE = (
    "You are a strict grader. Score the trajectory on [0, 1] and return JSON "
    "matching schema {score, rationale, flags}."
)


def _valid_output_json() -> str:
    """A response payload that satisfies ``JudgeOutput`` validation."""
    return json.dumps(
        {
            "score": 0.85,
            "rationale": "Trajectory met all acceptance criteria.",
            "flags": ["minor-style"],
        }
    )


def _populate_replay(
    store: ReplayStore,
    model: str,
    prompt: str,
    input_payload: dict,
    response_text: str,
    *,
    temperature: float = 0.0,
    seed: int | None = None,
    latency_ms: int = 42,
    cost_usd: float = 0.0001,
) -> JudgeCall:
    """Write a recording so subsequent ``LLMJudge.evaluate`` calls replay it."""
    call = JudgeCall(
        model=model,
        prompt=prompt,
        temperature=temperature,
        seed=seed,
        input_payload=input_payload,
    )
    store.record(
        call,
        response={"text": response_text},
        latency_ms=latency_ms,
        cost_usd=cost_usd,
    )
    return call


@pytest.fixture
def replay_store(tmp_path: Path) -> ReplayStore:
    return ReplayStore(tmp_path)


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'receipts.db'}"


@pytest.fixture
def upgraded_engine(db_url: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(cfg, "head")
    engine = create_engine(db_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session(upgraded_engine) -> Session:
    SessionFactory = sessionmaker(bind=upgraded_engine, expire_on_commit=False)
    with SessionFactory() as s:
        yield s


# ------------------------------ tests ------------------------------


def test_prompt_sha_matches_sha256_of_template(replay_store: ReplayStore) -> None:
    """``prompt_sha`` is the version registry primary key.

    The Merkle log persists this value with every judge call. Auditors
    correlate verdicts to prompts by recomputing sha256 of the deployed
    template -- so the property must equal the byte-exact sha256 hex of
    the prompt string passed to ``__init__``.
    """
    judge = LLMJudge(
        model="claude-opus-4-7",
        prompt_template=PROMPT_TEMPLATE,
        replay_store=replay_store,
    )
    expected = hashlib.sha256(PROMPT_TEMPLATE.encode("utf-8")).hexdigest()
    assert judge.prompt_sha == expected
    assert len(judge.prompt_sha) == 64


def test_evaluate_replay_mode_returns_validated_output(
    replay_store: ReplayStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replay path: pre-recorded JSON parses into a ``JudgeOutput``.

    No SDK clients are injected here; reaching the wire would be a defect.
    The replay-mode codepath must round-trip the stored ``response["text"]``
    through ``JudgeOutput.model_validate_json``.
    """
    monkeypatch.setenv("RECEIPTS_JUDGE_MODE", "replay")
    input_payload = {"trajectory_id": "t-7", "answer": "A"}
    _populate_replay(
        replay_store,
        "claude-opus-4-7",
        PROMPT_TEMPLATE,
        input_payload,
        _valid_output_json(),
    )

    judge = LLMJudge(
        model="claude-opus-4-7",
        prompt_template=PROMPT_TEMPLATE,
        replay_store=replay_store,
    )
    result = judge.evaluate(input_payload)

    assert isinstance(result, JudgeOutput)
    assert result.score == pytest.approx(0.85)
    assert result.rationale == "Trajectory met all acceptance criteria."
    assert result.flags == ["minor-style"]


def test_evaluate_replay_raises_on_invalid_schema(
    replay_store: ReplayStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Schema enforcement: a malformed recording must surface, not silently pass.

    Recordings that do not satisfy ``JudgeOutput`` (here: ``score`` out of
    range and ``rationale`` too short) are a corpus-rot signal -- we want
    the test suite to flag them at parse time rather than ship a bad
    verdict to the Merkle log.
    """
    monkeypatch.setenv("RECEIPTS_JUDGE_MODE", "replay")
    input_payload = {"trajectory_id": "bad-1"}
    bad_payload = json.dumps({"score": 9.0, "rationale": "x", "flags": []})
    _populate_replay(
        replay_store,
        "claude-opus-4-7",
        PROMPT_TEMPLATE,
        input_payload,
        bad_payload,
    )
    judge = LLMJudge(
        model="claude-opus-4-7",
        prompt_template=PROMPT_TEMPLATE,
        replay_store=replay_store,
    )
    with pytest.raises(ValidationError):
        judge.evaluate(input_payload)


def test_evaluate_appends_merkle_log_row(
    replay_store: ReplayStore,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Merkle log row is the durable audit trail.

    On every successful evaluate, the wrapper appends one attestation with
    ``kind="judge_call"`` carrying model, prompt_sha, request_hash,
    response_text, latency_ms, cost_usd. The chain must remain intact
    (``verify_chain() == []``) afterwards and exactly one new row must be
    present.
    """
    monkeypatch.setenv("RECEIPTS_JUDGE_MODE", "replay")
    input_payload = {"trajectory_id": "audit-1"}
    _populate_replay(
        replay_store,
        "claude-opus-4-7",
        PROMPT_TEMPLATE,
        input_payload,
        _valid_output_json(),
    )

    merkle = MerkleLog(session)
    before = session.query(Attestation).count()

    judge = LLMJudge(
        model="claude-opus-4-7",
        prompt_template=PROMPT_TEMPLATE,
        replay_store=replay_store,
        merkle_log=merkle,
    )
    judge.evaluate(input_payload)

    after = session.query(Attestation).count()
    assert after == before + 1
    assert merkle.verify_chain() == []

    row = session.query(Attestation).order_by(Attestation.id.desc()).first()
    assert row is not None
    assert row.kind == "judge_call"
    assert row.target_kind == "judge"
    assert row.payload["model"] == "claude-opus-4-7"
    assert (
        row.payload["prompt_sha"]
        == hashlib.sha256(PROMPT_TEMPLATE.encode("utf-8")).hexdigest()
    )
    # request_hash is the stable_hash over the JudgeCall, 64-char sha256 hex.
    assert len(row.payload["request_hash"]) == 64


def test_anthropic_adapter_omits_forbidden_kwargs() -> None:
    """``claude-opus-4-7`` rejects sampler-tuning kwargs.

    The model returns 400 if ``temperature``/``top_p``/``top_k``/
    ``budget_tokens`` are present. The adapter must call
    ``messages.create`` with only ``model``, ``max_tokens``, ``messages`` --
    any drift here breaks production calls silently in replay tests.
    """
    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=_valid_output_json())]
    client.messages.create.return_value = response

    raw, latency_ms, cost_usd = AnthropicAdapter.call(
        client,
        "claude-opus-4-7",
        PROMPT_TEMPLATE,
        {"trajectory_id": "kw-1"},
    )

    assert raw == _valid_output_json()
    assert isinstance(latency_ms, int)
    assert isinstance(cost_usd, float)

    client.messages.create.assert_called_once()
    _, kwargs = client.messages.create.call_args
    forbidden = {"temperature", "top_p", "top_k", "budget_tokens"}
    assert forbidden.isdisjoint(kwargs.keys()), (
        f"Anthropic adapter passed forbidden kwargs: {forbidden & kwargs.keys()}"
    )
    assert set(kwargs.keys()) == {"model", "max_tokens", "messages"}
    assert kwargs["model"] == "claude-opus-4-7"


def test_openai_adapter_uses_max_completion_tokens() -> None:
    """``gpt-5.4-*`` requires ``max_completion_tokens``.

    The legacy ``max_tokens`` kwarg is rejected. This test pins the wire
    contract so a future refactor that re-adds ``max_tokens`` for
    parity-with-Anthropic is caught at unit-test time.
    """
    client = MagicMock()
    response = MagicMock()
    choice = MagicMock()
    choice.message.content = _valid_output_json()
    response.choices = [choice]
    client.chat.completions.create.return_value = response

    raw, latency_ms, cost_usd = OpenAIAdapter.call(
        client,
        "gpt-5.4-2026-03-05",
        PROMPT_TEMPLATE,
        {"trajectory_id": "kw-2"},
    )

    assert raw == _valid_output_json()
    assert isinstance(latency_ms, int)
    assert isinstance(cost_usd, float)

    client.chat.completions.create.assert_called_once()
    _, kwargs = client.chat.completions.create.call_args
    assert "max_completion_tokens" in kwargs
    assert "max_tokens" not in kwargs


# ------------------- additional coverage ----------------------


def test_evaluate_record_mode_dispatches_anthropic(
    replay_store: ReplayStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Record-mode + claude-* prefix routes through ``AnthropicAdapter``.

    The injected mock client also doubles as the persistence smoke-test:
    after the call returns, ``replay_store.replay(call)`` must succeed --
    the wrapper writes the recording so subsequent replays are hermetic.
    """
    monkeypatch.setenv("RECEIPTS_JUDGE_MODE", "record")

    client = MagicMock()
    response = MagicMock()
    response.content = [MagicMock(text=_valid_output_json())]
    client.messages.create.return_value = response

    judge = LLMJudge(
        model="claude-opus-4-7",
        prompt_template=PROMPT_TEMPLATE,
        replay_store=replay_store,
        anthropic_client=client,
    )
    out = judge.evaluate({"trajectory_id": "rec-1"})
    assert isinstance(out, JudgeOutput)
    client.messages.create.assert_called_once()

    # Recording was written; future replays will not need the client.
    call = JudgeCall(
        model="claude-opus-4-7",
        prompt=PROMPT_TEMPLATE,
        temperature=0.0,
        seed=None,
        input_payload={"trajectory_id": "rec-1"},
    )
    rec = replay_store.replay(call)
    assert isinstance(rec, JudgeRecording)
    assert rec.response == {"text": _valid_output_json()}


def test_evaluate_record_mode_unknown_model_raises(
    replay_store: ReplayStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsupported model family in record-mode must raise rather than dispatch."""
    monkeypatch.setenv("RECEIPTS_JUDGE_MODE", "record")
    judge = LLMJudge(
        model="mistral-medium-2026",
        prompt_template=PROMPT_TEMPLATE,
        replay_store=replay_store,
    )
    with pytest.raises(ValueError, match="unsupported model"):
        judge.evaluate({"trajectory_id": "x"})
