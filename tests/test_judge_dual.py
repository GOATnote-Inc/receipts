"""Tests for J5: dual-judge agreement engine.

J5 orchestrates two J4 ``LLMJudge`` instances (typically ``claude-opus-4-7``
paired with ``gpt-5.4-2026-03-05``) over the same input payloads, buckets
each score to one decimal place, and computes Cohen's κ on the bucket
sequence. The κ ≥ 0.40 gate is the dual-judge half of the substrate stop
hook (CLAUDE.md "Stop hook gates").

The tests below pin five contracts:

1. ``evaluate_pair`` returns an ``AgreementRecord`` whose ``bucket_a`` /
   ``bucket_b`` are ``round(score, 1)`` and ``agree_strict`` is the bucket
   equality.
2. ``evaluate_batch`` aggregates buckets across cases and runs them
   through :func:`cohen_kappa`; the resulting ``DualJudgeResult`` reports
   ``gate_pass`` against the configured threshold.
3. High-agreement recordings drive κ ≥ 0.40 and ``gate_pass`` is True.
4. Low-agreement recordings drive κ < 0.40 and ``gate_pass`` is False.
5. A non-default ``threshold`` overrides the gate decision on the same data.

All judge calls are populated into a tmp-path ``ReplayStore`` ahead of
time; ``RECEIPTS_JUDGE_MODE`` defaults to ``"replay"`` so no SDK is
reached.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from receipts.judge import (
    AgreementRecord,
    DualJudge,
    DualJudgeResult,
    JudgeCall,
    LLMJudge,
    ReplayStore,
)

# --------------------------- helpers / fixtures ---------------------------


PROMPT_A = "Judge A: score [0,1] and emit {score, rationale, flags}."
PROMPT_B = "Judge B: score [0,1] and emit {score, rationale, flags}."

MODEL_A = "claude-opus-4-7"
MODEL_B = "gpt-5.4-2026-03-05"


def _record_output(
    store: ReplayStore,
    model: str,
    prompt: str,
    input_payload: dict,
    score: float,
    rationale: str = "Auto-generated rationale long enough to pass schema.",
    flags: list[str] | None = None,
) -> None:
    """Pre-populate a recording so an ``LLMJudge.evaluate`` call replays it.

    The wrapper builds ``JudgeCall(temperature=0.0, seed=None, ...)`` by
    default; we match that here so the stable-hash key lines up.
    """
    if flags is None:
        flags = []
    call = JudgeCall(
        model=model,
        prompt=prompt,
        temperature=0.0,
        seed=None,
        input_payload=input_payload,
    )
    response_text = json.dumps({"score": score, "rationale": rationale, "flags": flags})
    store.record(
        call,
        response={"text": response_text},
        latency_ms=42,
        cost_usd=0.0001,
    )


def _make_dual(
    store: ReplayStore,
    threshold: float = 0.40,
) -> DualJudge:
    """Construct a ``DualJudge`` over the shared replay store."""
    judge_a = LLMJudge(
        model=MODEL_A,
        prompt_template=PROMPT_A,
        replay_store=store,
    )
    judge_b = LLMJudge(
        model=MODEL_B,
        prompt_template=PROMPT_B,
        replay_store=store,
    )
    return DualJudge(judge_a, judge_b, threshold=threshold)


@pytest.fixture
def replay_store(tmp_path: Path) -> ReplayStore:
    return ReplayStore(tmp_path)


@pytest.fixture(autouse=True)
def _default_replay_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force replay mode for every test in this module."""
    monkeypatch.setenv("RECEIPTS_JUDGE_MODE", "replay")


# ------------------------------ tests ------------------------------


def test_evaluate_pair_returns_record(replay_store: ReplayStore) -> None:
    """``evaluate_pair`` packs both judges' outputs into an ``AgreementRecord``.

    When both judges return identical scores, ``agree_strict`` must be
    True, and the buckets must equal the rounded scores.
    """
    payload = {"trajectory_id": "case-pair"}
    _record_output(replay_store, MODEL_A, PROMPT_A, payload, 0.80, flags=["a-flag"])
    _record_output(replay_store, MODEL_B, PROMPT_B, payload, 0.80, flags=["b-flag"])

    dual = _make_dual(replay_store)
    record = dual.evaluate_pair("case-pair", payload)

    assert isinstance(record, AgreementRecord)
    assert record.case_id == "case-pair"
    assert record.score_a == pytest.approx(0.80)
    assert record.score_b == pytest.approx(0.80)
    assert record.bucket_a == pytest.approx(0.8)
    assert record.bucket_b == pytest.approx(0.8)
    assert record.agree_strict is True
    assert record.flags_a == ["a-flag"]
    assert record.flags_b == ["b-flag"]


def test_evaluate_batch_high_agreement_passes_gate(
    replay_store: ReplayStore,
) -> None:
    """High-agreement recordings drive κ above 0.40 and the gate passes.

    Four cases, identical bucket pairs across multiple distinct buckets:
    P_o = 1.0, P_e < 1.0 → κ = 1.0, well above the 0.40 threshold.
    """
    cases = [
        ("c-1", {"trajectory_id": "c-1"}, 0.80, 0.80),
        ("c-2", {"trajectory_id": "c-2"}, 0.60, 0.60),
        ("c-3", {"trajectory_id": "c-3"}, 0.30, 0.30),
        ("c-4", {"trajectory_id": "c-4"}, 0.90, 0.90),
    ]
    for _, payload, score_a, score_b in cases:
        _record_output(replay_store, MODEL_A, PROMPT_A, payload, score_a)
        _record_output(replay_store, MODEL_B, PROMPT_B, payload, score_b)

    dual = _make_dual(replay_store)
    result = dual.evaluate_batch([(cid, payload) for cid, payload, *_ in cases])

    assert isinstance(result, DualJudgeResult)
    assert result.n_cases == 4
    assert result.kappa == pytest.approx(1.0)
    assert result.threshold == pytest.approx(0.40)
    assert result.gate_pass is True
    assert len(result.agreement_records) == 4
    assert all(r.agree_strict for r in result.agreement_records)


def test_evaluate_batch_low_agreement_fails_gate(
    replay_store: ReplayStore,
) -> None:
    """Low-agreement recordings drive κ below 0.40 and the gate fails.

    Five cases with one match (case 5). Both raters span five distinct
    buckets with overlap on only four labels at one observation each:
    P_o = 1/5 = 0.20; P_e = 4 * (1/5)(1/5) = 0.16; κ ≈ 0.0476 < 0.40.
    """
    cases = [
        ("c-1", {"trajectory_id": "c-1"}, 0.80, 0.70),
        ("c-2", {"trajectory_id": "c-2"}, 0.60, 0.50),
        ("c-3", {"trajectory_id": "c-3"}, 0.50, 0.60),
        ("c-4", {"trajectory_id": "c-4"}, 0.40, 0.80),
        ("c-5", {"trajectory_id": "c-5"}, 0.30, 0.30),
    ]
    for _, payload, score_a, score_b in cases:
        _record_output(replay_store, MODEL_A, PROMPT_A, payload, score_a)
        _record_output(replay_store, MODEL_B, PROMPT_B, payload, score_b)

    dual = _make_dual(replay_store)
    result = dual.evaluate_batch([(cid, payload) for cid, payload, *_ in cases])

    assert result.n_cases == 5
    # (0.20 - 0.16) / (1 - 0.16) = 0.04 / 0.84 ≈ 0.04761904761904762
    assert result.kappa == pytest.approx(0.04761904761904762, abs=1e-9)
    assert result.gate_pass is False
    assert result.threshold == pytest.approx(0.40)


def test_bucketing_rounds_to_one_decimal(replay_store: ReplayStore) -> None:
    """``bucket_a`` / ``bucket_b`` are ``round(score, 1)`` for strict equality.

    Two pair-checks pin both branches of the bucketing rule:
    - 0.61 vs 0.69 → 0.6 vs 0.7 → ``agree_strict`` False
    - 0.71 vs 0.74 → 0.7 vs 0.7 → ``agree_strict`` True
    """
    # Branch 1: scores round to different buckets.
    payload_diff = {"trajectory_id": "round-diff"}
    _record_output(replay_store, MODEL_A, PROMPT_A, payload_diff, 0.61)
    _record_output(replay_store, MODEL_B, PROMPT_B, payload_diff, 0.69)

    dual = _make_dual(replay_store)
    rec_diff = dual.evaluate_pair("round-diff", payload_diff)
    assert rec_diff.bucket_a == pytest.approx(0.6)
    assert rec_diff.bucket_b == pytest.approx(0.7)
    assert rec_diff.agree_strict is False

    # Branch 2: scores round to the same bucket.
    payload_same = {"trajectory_id": "round-same"}
    _record_output(replay_store, MODEL_A, PROMPT_A, payload_same, 0.71)
    _record_output(replay_store, MODEL_B, PROMPT_B, payload_same, 0.74)

    rec_same = dual.evaluate_pair("round-same", payload_same)
    assert rec_same.bucket_a == pytest.approx(0.7)
    assert rec_same.bucket_b == pytest.approx(0.7)
    assert rec_same.agree_strict is True


def test_threshold_override(replay_store: ReplayStore) -> None:
    """A custom threshold flips ``gate_pass`` on the same recordings.

    Re-use the low-agreement fixture (κ ≈ 0.0476). At the default 0.40
    threshold the gate fails; dropping the threshold below the measured
    κ flips it to ``True``.
    """
    cases = [
        ("c-1", {"trajectory_id": "c-1"}, 0.80, 0.70),
        ("c-2", {"trajectory_id": "c-2"}, 0.60, 0.50),
        ("c-3", {"trajectory_id": "c-3"}, 0.50, 0.60),
        ("c-4", {"trajectory_id": "c-4"}, 0.40, 0.80),
        ("c-5", {"trajectory_id": "c-5"}, 0.30, 0.30),
    ]
    for _, payload, score_a, score_b in cases:
        _record_output(replay_store, MODEL_A, PROMPT_A, payload, score_a)
        _record_output(replay_store, MODEL_B, PROMPT_B, payload, score_b)

    batch = [(cid, payload) for cid, payload, *_ in cases]

    dual_default = _make_dual(replay_store, threshold=0.40)
    res_default = dual_default.evaluate_batch(batch)
    assert res_default.gate_pass is False

    dual_relaxed = _make_dual(replay_store, threshold=0.0)
    res_relaxed = dual_relaxed.evaluate_batch(batch)
    assert res_relaxed.kappa == pytest.approx(res_default.kappa, abs=1e-12)
    assert res_relaxed.threshold == pytest.approx(0.0)
    assert res_relaxed.gate_pass is True
