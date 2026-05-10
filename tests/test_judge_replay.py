"""Tests for judge replay mode (J7).

The L2 LLM layer of the CEIS judge stack is the only non-deterministic step
in the receipts pipeline. To keep the test suite hermetic and cheap, J7
provides a record/replay store keyed by a stable hash over (model, prompt,
temperature, seed, input_payload).

Contract:
- ``stable_hash`` is purely a function of the call inputs and is identical
  across processes (sha256 over canonical JSON, sort_keys, no whitespace).
- ``ReplayStore.record`` writes ``{hash}.json`` to its path; ``replay``
  reads the same file and reconstructs the full ``JudgeRecording``.
- ``ReplayStore.mode_from_env`` defaults to ``"replay"`` so CI never makes
  live judge calls without an explicit opt-in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from receipts.judge import (
    JudgeCall,
    JudgeRecording,
    ReplayStore,
    stable_hash,
)


def _make_call(input_payload: dict | None = None) -> JudgeCall:
    return JudgeCall(
        model="claude-opus-4-7",
        prompt="grade this trajectory",
        temperature=0.0,
        seed=42,
        input_payload=input_payload if input_payload is not None else {"task_id": "t-1"},
    )


# ----------------------------- stable_hash -----------------------------


def test_stable_hash_deterministic() -> None:
    # Same call inputs must yield identical hash across separate constructions.
    call_a = _make_call()
    call_b = _make_call()
    assert stable_hash(call_a) == stable_hash(call_b)
    # Hash must be a sha256 hex digest (64 hex chars).
    h = stable_hash(call_a)
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_stable_hash_changes_with_input() -> None:
    # Varying any field in input_payload must change the hash.
    base = _make_call(input_payload={"task_id": "t-1", "answer": "A"})
    diff = _make_call(input_payload={"task_id": "t-1", "answer": "B"})
    assert stable_hash(base) != stable_hash(diff)
    # Same content with reordered keys must collide (canonical JSON sorts keys).
    reordered = JudgeCall(
        model=base.model,
        prompt=base.prompt,
        temperature=base.temperature,
        seed=base.seed,
        input_payload={"answer": "A", "task_id": "t-1"},
    )
    assert stable_hash(base) == stable_hash(reordered)


def test_stable_hash_changes_with_model_and_temperature() -> None:
    base = _make_call()
    other_model = JudgeCall(
        model="claude-sonnet-4-7",
        prompt=base.prompt,
        temperature=base.temperature,
        seed=base.seed,
        input_payload=base.input_payload,
    )
    other_temp = JudgeCall(
        model=base.model,
        prompt=base.prompt,
        temperature=0.7,
        seed=base.seed,
        input_payload=base.input_payload,
    )
    assert stable_hash(base) != stable_hash(other_model)
    assert stable_hash(base) != stable_hash(other_temp)


# ----------------------------- ReplayStore -----------------------------


def test_record_then_replay_roundtrip(tmp_path: Path) -> None:
    store = ReplayStore(tmp_path)
    call = _make_call()
    response = {"verdict": "pass", "rationale": "well-grounded"}
    store.record(call, response=response, latency_ms=123, cost_usd=0.0042)

    rec = store.replay(call)
    assert isinstance(rec, JudgeRecording)
    assert rec.call == call
    assert rec.response == response
    assert rec.latency_ms == 123
    assert rec.cost_usd == pytest.approx(0.0042)
    # recorded_at is an ISO8601 string we can re-parse.
    from datetime import datetime

    parsed = datetime.fromisoformat(rec.recorded_at)
    assert parsed.tzinfo is not None  # always UTC-stamped


def test_replay_is_deterministic_across_store_instances(tmp_path: Path) -> None:
    # Two separate ReplayStore objects pointed at the same path must read
    # back identical recordings -- this is the cross-process determinism
    # guarantee that lets CI replay fixtures captured locally.
    call = _make_call()
    response = {"verdict": "fail"}

    ReplayStore(tmp_path).record(call, response=response, latency_ms=7, cost_usd=0.001)

    rec_a = ReplayStore(tmp_path).replay(call)
    rec_b = ReplayStore(tmp_path).replay(call)
    assert rec_a == rec_b
    assert rec_a.response == response


def test_replay_raises_on_missing_recording(tmp_path: Path) -> None:
    store = ReplayStore(tmp_path)
    call = _make_call()
    with pytest.raises(FileNotFoundError):
        store.replay(call)


def test_record_writes_hash_named_json(tmp_path: Path) -> None:
    # The on-disk fixture filename must be exactly ``{stable_hash}.json`` so
    # downstream tooling can locate recordings by recomputing the hash.
    store = ReplayStore(tmp_path)
    call = _make_call()
    store.record(call, response={"ok": True}, latency_ms=1, cost_usd=0.0)

    expected = tmp_path / f"{stable_hash(call)}.json"
    assert expected.exists()


# ----------------------------- mode_from_env -----------------------------


def test_mode_from_env_default_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECEIPTS_JUDGE_MODE", raising=False)
    assert ReplayStore.mode_from_env() == "replay"


def test_mode_from_env_explicit_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RECEIPTS_JUDGE_MODE", "record")
    assert ReplayStore.mode_from_env() == "record"
