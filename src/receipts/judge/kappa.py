"""Cohen's kappa and Wilson score confidence interval.

Pure-Python, no numpy / scipy / sklearn at runtime. Both functions match
their canonical reference implementations to ~1e-9 absolute tolerance for
typical inputs (verified offline against scipy 1.13 / sklearn 1.5).

Why this lives in receipts.judge:
- κ ≥ 0.40 is the dual-judge stop-hook gate (CLAUDE.md "Stop hook gates").
- Wilson CI is the small-sample interval used wherever we report a binomial
  proportion in the run log (pass^k, safety-fail rate, judge agreement).
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Sequence

# z_{α/2} for α = 0.05 (two-sided 95% interval).
# Equal to math.sqrt(2.0) * scipy.special.erfcinv(0.05) at double precision.
_Z_95 = 1.959963984540054


def cohen_kappa(rater_a: Sequence[Hashable], rater_b: Sequence[Hashable]) -> float:
    """Compute Cohen's κ for two raters over a paired categorical sequence.

    Args:
        rater_a: Hashable category labels from rater A.
        rater_b: Hashable category labels from rater B, same length as ``rater_a``.

    Returns:
        κ = (P_o − P_e) / (1 − P_e), where P_o is observed agreement and
        P_e is the chance-agreement implied by per-rater marginals.

    Raises:
        ValueError: lengths differ, or both sequences are empty.

    Notes:
        - Degenerate case P_e == 1.0 with P_o == 1.0 (both raters constant on
          the same single label) returns 1.0 by convention. This matches the
          intuition "they always agreed" and avoids surfacing NaN through the
          stop-hook gate.
        - Degenerate case P_e == 1.0 with P_o < 1.0 is mathematically
          impossible for the standard two-rater formulation: if every chance
          pair is a hit, every observed pair must be a hit too. We therefore
          do not need a separate exception path for that branch, but a defensive
          ZeroDivisionError-safe message is raised if a future change to the
          formula ever produces it.
    """
    n = len(rater_a)
    if n != len(rater_b):
        raise ValueError(
            f"cohen_kappa: rater_a and rater_b must have the same length "
            f"(got {n} and {len(rater_b)})"
        )
    if n == 0:
        raise ValueError("cohen_kappa: input sequences are empty")

    categories: set[Hashable] = set(rater_a) | set(rater_b)

    # Observed agreement.
    matches = sum(1 for x, y in zip(rater_a, rater_b, strict=True) if x == y)
    p_o = matches / n

    # Marginal probability per rater per category.
    a_marg: dict[Hashable, float] = dict.fromkeys(categories, 0.0)
    b_marg: dict[Hashable, float] = dict.fromkeys(categories, 0.0)
    for x in rater_a:
        a_marg[x] += 1.0 / n
    for y in rater_b:
        b_marg[y] += 1.0 / n

    p_e = sum(a_marg[c] * b_marg[c] for c in categories)

    if p_e >= 1.0:
        # Both marginals collapse onto a single shared label. Spec says return 1.0
        # for the perfect-agreement case; the disagreement branch is unreachable
        # under the standard two-rater formula but we guard against future drift.
        if p_o >= 1.0:
            return 1.0
        raise ValueError(
            "cohen_kappa: degenerate P_e == 1.0 with observed disagreement "
            "(mathematically impossible under standard formulation)"
        )

    return (p_o - p_e) / (1.0 - p_e)


def wilson_ci(successes: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Wilson score confidence interval for a binomial proportion.

    Newcombe 1998 / standard Wilson construction. Returns the two-sided
    1 − α interval, clamped to ``[0, 1]``.

    Args:
        successes: Number of observed successes (must satisfy 0 ≤ k ≤ n).
        n: Total number of trials (must be ≥ 1).
        alpha: Significance level (default 0.05 → 95% CI).

    Returns:
        ``(low, high)`` with ``0.0 ≤ low ≤ high ≤ 1.0``.

    Raises:
        ValueError: n < 1, successes < 0, or successes > n.
    """
    if n < 1:
        raise ValueError(f"wilson_ci: n must be >= 1 (got {n})")
    if successes < 0:
        raise ValueError(f"wilson_ci: successes must be >= 0 (got {successes})")
    if successes > n:
        raise ValueError(f"wilson_ci: successes must be <= n (got {successes} > {n})")

    # Hot path: alpha = 0.05 uses a hardcoded high-precision z_{0.025}.
    # General case inverts erfc to get z_{α/2} such that
    # Phi(z) = 1 - alpha/2  <=>  erfc(z / sqrt(2)) = alpha.
    # Bisection inside _inverse_erfc gives full double precision for our use.
    z = _Z_95 if alpha == 0.05 else math.sqrt(2.0) * _inverse_erfc(alpha)

    p = successes / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    margin = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denom

    # Clamp to [0, 1] per Newcombe 1998. We also snap exact-boundary cases
    # (successes == 0 / successes == n) to hard 0.0 / 1.0 to avoid tiny
    # floating-point residuals leaking through (e.g. 3.5e-18 from the
    # center-minus-margin subtraction at k=0).
    low = 0.0 if successes == 0 else max(0.0, center - margin)
    high = 1.0 if successes == n else min(1.0, center + margin)
    return low, high


def _inverse_erfc(y: float) -> float:
    """Numerical inverse of math.erfc, via bisection. Internal helper."""
    if not 0.0 < y < 2.0:
        raise ValueError(f"_inverse_erfc: y must be in (0, 2) (got {y})")
    lo, hi = -10.0, 10.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if math.erfc(mid) > y:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)
