"""Dual-judge agreement engine (J5).

J5 pairs two J4 :class:`LLMJudge` wrappers (typically ``claude-opus-4-7``
with ``gpt-5.4-2026-03-05``) over the same input payloads, buckets each
LLM score to one decimal place, and runs the bucketed sequence through
:func:`cohen_kappa`. The resulting :class:`DualJudgeResult` exposes the
agreement metric, the per-case audit trail, and a ``gate_pass`` flag
that the stop-hook uses to enforce the ``κ ≥ 0.40`` substrate rule
(CLAUDE.md "Stop hook gates").

Design notes
------------
- Replay-by-default. Both inner judges read ``RECEIPTS_JUDGE_MODE`` via
  the wrapper they already own; J5 does not re-implement that policy.
  ``make test`` is therefore hermetic as long as recordings exist.
- Bucketing is ``round(score, 1)`` — coarse enough that small judge-level
  jitter does not punish κ, fine enough that gross disagreement
  (e.g. "PASS at 0.85" vs "borderline at 0.55") still shows up as a
  distinct category. This matches the V8/V9 κ-overlay analysis in
  ``healthcraft`` (V9 overlay κ = 0.402 after tightening; see MEMORY.md).
- ``AgreementRecord`` carries both score floats *and* both bucket floats
  so the audit trail records what was scored *and* what got compared.
- ``DualJudgeResult.gate_pass`` evaluates ``kappa >= threshold`` (note:
  inclusive — the published gate is "κ ≥ 0.40", not strict >).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from receipts.judge.kappa import cohen_kappa
from receipts.judge.l2 import LLMJudge


@dataclass(frozen=True)
class AgreementRecord:
    """Per-case dual-judge audit row.

    ``bucket_a`` / ``bucket_b`` are ``round(score, 1)`` and form the
    categorical sequence fed to :func:`cohen_kappa`. ``agree_strict`` is
    the boolean bucket-equality, kept on the record so downstream
    dashboards do not have to recompute it.
    """

    case_id: str
    score_a: float
    score_b: float
    bucket_a: float
    bucket_b: float
    agree_strict: bool
    flags_a: list[str] = field(default_factory=list)
    flags_b: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DualJudgeResult:
    """Batch-level dual-judge verdict.

    ``kappa`` is the Cohen κ over the bucket sequence; ``gate_pass`` is
    the boolean ``kappa >= threshold``. ``agreement_records`` retains
    every per-case row so the stop hook (and any later audit) can drill
    in on the disagreements that pulled κ down.
    """

    kappa: float
    n_cases: int
    gate_pass: bool
    threshold: float
    agreement_records: list[AgreementRecord] = field(default_factory=list)


class DualJudge:
    """Orchestrates two :class:`LLMJudge` instances over the same inputs.

    The wrapper is intentionally thin: it does not inject ``RECEIPTS_JUDGE_MODE``,
    construct SDK clients, or persist Merkle rows. Those responsibilities
    live in :class:`LLMJudge` (J4); J5 layers κ + gate semantics on top.

    The default ``threshold`` (0.40) mirrors the CLAUDE.md stop-hook
    requirement so a caller that omits the kwarg gets the production
    gate by construction.
    """

    def __init__(
        self,
        judge_a: LLMJudge,
        judge_b: LLMJudge,
        threshold: float = 0.40,
    ) -> None:
        self.judge_a = judge_a
        self.judge_b = judge_b
        self.threshold = threshold

    def evaluate_pair(self, case_id: str, input_payload: dict) -> AgreementRecord:
        """Score ``input_payload`` with both inner judges and pack the result.

        Both judges are dispatched (replay mode by default) and the
        returned scores get bucketed to one decimal. ``agree_strict`` is
        the bucket equality, so two judges that round to the same
        bucket count as agreement even if their raw scores differ.
        """
        output_a = self.judge_a.evaluate(input_payload)
        output_b = self.judge_b.evaluate(input_payload)

        bucket_a = round(output_a.score, 1)
        bucket_b = round(output_b.score, 1)

        return AgreementRecord(
            case_id=case_id,
            score_a=output_a.score,
            score_b=output_b.score,
            bucket_a=bucket_a,
            bucket_b=bucket_b,
            agree_strict=bucket_a == bucket_b,
            flags_a=list(output_a.flags),
            flags_b=list(output_b.flags),
        )

    def evaluate_batch(
        self,
        cases: list[tuple[str, dict]],
    ) -> DualJudgeResult:
        """Run :meth:`evaluate_pair` over every case and aggregate to κ.

        Cases are processed in order; the bucket sequences for raters A
        and B are fed to :func:`cohen_kappa` as parallel lists.
        ``gate_pass`` is the inclusive comparison ``kappa >= threshold``
        so the stop hook fires only when agreement drops below the
        published bar.
        """
        records: list[AgreementRecord] = []
        for case_id, payload in cases:
            records.append(self.evaluate_pair(case_id, payload))

        buckets_a = [r.bucket_a for r in records]
        buckets_b = [r.bucket_b for r in records]
        kappa = cohen_kappa(buckets_a, buckets_b)

        return DualJudgeResult(
            kappa=kappa,
            n_cases=len(records),
            gate_pass=kappa >= self.threshold,
            threshold=self.threshold,
            agreement_records=records,
        )


__all__ = [
    "AgreementRecord",
    "DualJudge",
    "DualJudgeResult",
]
