"""Judge subsystem: CEIS scoring layers, kappa, dual-judge orchestration."""

from receipts.judge.dual_judge import AgreementRecord, DualJudge, DualJudgeResult
from receipts.judge.kappa import cohen_kappa, wilson_ci
from receipts.judge.l0 import Issue, RuleRegistry, run_rules
from receipts.judge.l1 import StructuralResult, score_structure
from receipts.judge.l2 import (
    AnthropicAdapter,
    JudgeOutput,
    LLMJudge,
    OpenAIAdapter,
)
from receipts.judge.passk import (
    PasskResult,
    TrialResult,
    compute_passk,
    compute_passk_detailed,
)
from receipts.judge.replay import (
    JudgeCall,
    JudgeRecording,
    ReplayStore,
    stable_hash,
)

__all__ = [
    "AgreementRecord",
    "AnthropicAdapter",
    "DualJudge",
    "DualJudgeResult",
    "Issue",
    "JudgeCall",
    "JudgeOutput",
    "JudgeRecording",
    "LLMJudge",
    "OpenAIAdapter",
    "PasskResult",
    "ReplayStore",
    "RuleRegistry",
    "StructuralResult",
    "TrialResult",
    "cohen_kappa",
    "compute_passk",
    "compute_passk_detailed",
    "run_rules",
    "score_structure",
    "stable_hash",
    "wilson_ci",
]
