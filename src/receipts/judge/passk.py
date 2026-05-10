"""pass^k deployment-gate metric.

For ``k`` trials of the same task, ``pass^k`` is the fraction of tasks where
ALL ``k`` trials passed. Originated as the scribegoat2 deployment gate
(``pass^5 >= 0.95``) and is reused here for the receipts substrate stop hook
("Stop hook gates" in CLAUDE.md).

Why this lives in ``receipts.judge``:
- pass^k is computed over the same per-trial judged outputs that CEIS L0/L1/L2
  emit, so the canonical home is the judge subsystem.
- Pure stdlib (logging, dataclasses, collections). No numpy/pandas at runtime.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrialResult:
    """Single (task, trial) pass/fail observation.

    Trials are identified by ``(task_id, trial)``. ``trial`` is an integer
    in ``[0, k)``; duplicate ``(task_id, trial)`` pairs surface as a
    ``ValueError`` from :func:`compute_passk` to avoid silent corruption.
    """

    task_id: str
    trial: int
    passed: bool


@dataclass(frozen=True)
class PasskResult:
    """Full breakdown returned by :func:`compute_passk_detailed`.

    Attributes:
        passk: ``tasks_all_pass / tasks_total`` in ``[0.0, 1.0]``.
        tasks_total: Tasks with exactly ``k`` trials (the denominator).
        tasks_excluded: Tasks dropped for having ``!= k`` trials.
        tasks_all_pass: Tasks where every one of the ``k`` trials passed.
    """

    passk: float
    tasks_total: int
    tasks_excluded: int
    tasks_all_pass: int


def _group_by_task(
    results: list[TrialResult],
) -> dict[str, list[TrialResult]]:
    """Group trials by ``task_id`` and detect duplicate ``(task_id, trial)`` pairs.

    Raises:
        ValueError: a ``(task_id, trial)`` pair appears more than once.
    """
    grouped: dict[str, list[TrialResult]] = defaultdict(list)
    seen: dict[str, set[int]] = defaultdict(set)
    for r in results:
        if r.trial in seen[r.task_id]:
            raise ValueError(
                f"compute_passk: duplicate trial index {r.trial} for task "
                f"{r.task_id!r} (each (task_id, trial) pair must be unique)"
            )
        seen[r.task_id].add(r.trial)
        grouped[r.task_id].append(r)
    return grouped


def compute_passk_detailed(results: list[TrialResult], k: int) -> PasskResult:
    """Compute pass^k with a full breakdown of the underlying counts.

    Args:
        results: All ``(task_id, trial, passed)`` observations across the run.
        k: Required trials per task. Tasks with ``!= k`` trials are excluded
           with a ``logging.warning`` and counted in ``tasks_excluded``.

    Returns:
        :class:`PasskResult` capturing pass^k plus the eligible/excluded/
        all-pass counts.

    Raises:
        ValueError: ``k < 1``; duplicate ``(task_id, trial)`` pairs; or no
            task in ``results`` has exactly ``k`` trials (denominator would
            be zero -- the caller almost certainly has the wrong ``k`` or
            an empty corpus, so we surface it rather than silently return
            ``0.0`` and pass the gate).
    """
    if k < 1:
        raise ValueError(f"compute_passk: k must be >= 1 (got {k})")

    grouped = _group_by_task(results)

    tasks_total = 0
    tasks_excluded = 0
    tasks_all_pass = 0

    for task_id, trials in grouped.items():
        if len(trials) != k:
            tasks_excluded += 1
            logger.warning(
                "compute_passk: excluding task %r with %d trials (expected k=%d)",
                task_id,
                len(trials),
                k,
            )
            continue
        tasks_total += 1
        if all(t.passed for t in trials):
            tasks_all_pass += 1

    if tasks_total == 0:
        raise ValueError(
            f"compute_passk: no tasks with exactly k={k} trials found "
            f"(tasks_excluded={tasks_excluded})"
        )

    return PasskResult(
        passk=tasks_all_pass / tasks_total,
        tasks_total=tasks_total,
        tasks_excluded=tasks_excluded,
        tasks_all_pass=tasks_all_pass,
    )


def compute_passk(results: list[TrialResult], k: int) -> float:
    """Compute pass^k as a single scalar.

    Thin wrapper over :func:`compute_passk_detailed` for callers that only
    need the gate value; see that function for the precise contract on
    excluded tasks, duplicates, and the empty-denominator failure mode.
    """
    return compute_passk_detailed(results, k=k).passk
