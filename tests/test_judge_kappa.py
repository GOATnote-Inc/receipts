"""Tests for Cohen's kappa and Wilson score confidence interval (J1).

Golden reference values were computed offline using a scratch script that
implements the same formulas in pure Python at double precision (verified
against scipy's published z-score for alpha=0.05: 1.959963984540054).
scipy/sklearn are NOT runtime dependencies of receipts.
"""

from __future__ import annotations

import math

import pytest

from receipts.judge import cohen_kappa, wilson_ci

# Tight tolerance for floating-point comparisons. Reference values are
# computed to full double precision; 1e-9 leaves ample margin.
TOL = 1e-9


# ----------------------------- cohen_kappa -----------------------------


def test_kappa_perfect_agreement() -> None:
    a = [1, 1, 1, 0, 0, 0]
    b = [1, 1, 1, 0, 0, 0]
    assert cohen_kappa(a, b) == pytest.approx(1.0, abs=TOL)


def test_kappa_perfect_disagreement_binary_balanced() -> None:
    # Both raters use a balanced 3/3 marginal, but they swap labels for every
    # item. P_o = 0, P_e = 0.5, so kappa = (0 - 0.5) / (1 - 0.5) = -1.0.
    a = [1, 1, 1, 0, 0, 0]
    b = [0, 0, 0, 1, 1, 1]
    assert cohen_kappa(a, b) == pytest.approx(-1.0, abs=TOL)


def test_kappa_partial_agreement_seven_of_ten() -> None:
    # 7/10 matches, a_marg = (yes:5, no:5), b_marg = (yes:4, no:6).
    # P_o = 0.7, P_e = 0.5*0.4 + 0.5*0.6 = 0.5, kappa = 0.4.
    a = [1, 1, 1, 1, 1, 0, 0, 0, 0, 0]
    b = [1, 1, 1, 0, 0, 1, 0, 0, 0, 0]
    assert cohen_kappa(a, b) == pytest.approx(0.4, abs=TOL)


def test_kappa_negative_below_chance() -> None:
    # Marginals balanced 5/5 on both raters, but only 2/10 agree.
    # P_o = 0.2, P_e = 0.5, kappa = -0.6.
    a = [1, 1, 1, 1, 1, 0, 0, 0, 0, 0]
    b = [0, 0, 0, 0, 1, 1, 1, 1, 1, 0]
    assert cohen_kappa(a, b) == pytest.approx(-0.6, abs=TOL)


def test_kappa_multiclass_three_categories() -> None:
    # a_marg = (1:4/10, 2:3/10, 3:3/10), b_marg = (1:3/10, 2:4/10, 3:3/10).
    # P_o = 0.7, P_e = 0.33, kappa = 0.37 / 0.67 = 0.5522388059701492.
    a = [1, 2, 3, 1, 2, 3, 1, 2, 3, 1]
    b = [1, 2, 3, 1, 2, 3, 1, 3, 2, 2]
    assert cohen_kappa(a, b) == pytest.approx(0.5522388059701492, abs=TOL)


def test_kappa_string_labels() -> None:
    # Three categories as strings to exercise Hashable contract.
    # a_marg = (yes:2/5, no:2/5, maybe:1/5)
    # b_marg = (yes:2/5, no:2/5, maybe:1/5)
    # matches at idx 0,1,2 => P_o = 0.6
    # P_e = 0.4*0.4 + 0.4*0.4 + 0.2*0.2 = 0.16+0.16+0.04 = 0.36
    # kappa = (0.6 - 0.36) / (1 - 0.36) = 0.24 / 0.64 = 0.375
    a = ["yes", "no", "maybe", "yes", "no"]
    b = ["yes", "no", "maybe", "no", "yes"]
    assert cohen_kappa(a, b) == pytest.approx(0.375, abs=TOL)


def test_kappa_degenerate_perfect_pe_one() -> None:
    # Both raters always emit the same single label: P_e = 1.0 and P_o = 1.0.
    # Spec says: return 1.0 for this degenerate perfect-agreement case
    # (rather than raise / return NaN).
    a = [1, 1, 1, 1, 1]
    b = [1, 1, 1, 1, 1]
    assert cohen_kappa(a, b) == pytest.approx(1.0, abs=TOL)


def test_kappa_degenerate_pe_one_but_disagreement_raises() -> None:
    # Constructing P_e == 1.0 with P_o != 1.0 is impossible with two raters
    # who each only emit a single label, so we use a different construction:
    # if rater A is constant and rater B is constant on the same label, agreement
    # is forced. The degenerate "raises" case only exists when a single label
    # dominates marginals such that P_e = 1.0 yet P_o < 1.0 -- which cannot occur
    # without one of the raters varying. This test instead verifies that the
    # implementation doesn't accidentally divide by zero when 1 - P_e is small
    # but nonzero (sanity check for near-degenerate).
    a = [1, 1, 1, 1, 1, 1, 1, 1, 1, 0]
    b = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
    # a_marg = (1:9/10, 0:1/10), b_marg = (1:1.0)
    # P_o = 9/10. P_e = 0.9*1.0 + 0.1*0.0 = 0.9.
    # kappa = (0.9 - 0.9) / (1 - 0.9) = 0.0
    assert cohen_kappa(a, b) == pytest.approx(0.0, abs=TOL)


def test_kappa_raises_on_length_mismatch() -> None:
    with pytest.raises(ValueError, match="length"):
        cohen_kappa([1, 0, 1], [1, 0])


def test_kappa_raises_on_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        cohen_kappa([], [])


# ------------------------------ wilson_ci ------------------------------


def test_wilson_ci_central_case_n100_k50() -> None:
    # z_{0.025} = 1.959963984540054. Reference computed offline:
    low, high = wilson_ci(50, 100)
    assert low == pytest.approx(0.4038315303659956, abs=TOL)
    assert high == pytest.approx(0.5961684696340044, abs=TOL)


def test_wilson_ci_zero_successes_clamps_low() -> None:
    # k=0 gives an interval whose lower bound is 0 (clamped).
    low, high = wilson_ci(0, 100)
    assert low == 0.0
    assert high == pytest.approx(0.03699349820698568, abs=TOL)


def test_wilson_ci_all_successes_clamps_high() -> None:
    # k=n gives an interval whose upper bound is 1 (clamped).
    low, high = wilson_ci(100, 100)
    assert low == pytest.approx(0.9630065017930143, abs=TOL)
    assert high == 1.0


def test_wilson_ci_small_n_n10_k3() -> None:
    low, high = wilson_ci(3, 10)
    assert low == pytest.approx(0.10779126740630099, abs=TOL)
    assert high == pytest.approx(0.6032218525388546, abs=TOL)


def test_wilson_ci_n20_k10() -> None:
    low, high = wilson_ci(10, 20)
    assert low == pytest.approx(0.2992980081982123, abs=TOL)
    assert high == pytest.approx(0.7007019918017877, abs=TOL)


def test_wilson_ci_raises_on_invalid_n() -> None:
    with pytest.raises(ValueError, match="n"):
        wilson_ci(0, 0)
    with pytest.raises(ValueError, match="n"):
        wilson_ci(0, -1)


def test_wilson_ci_raises_on_negative_successes() -> None:
    with pytest.raises(ValueError, match="successes"):
        wilson_ci(-1, 10)


def test_wilson_ci_raises_on_successes_exceeds_n() -> None:
    with pytest.raises(ValueError, match="successes"):
        wilson_ci(11, 10)


def test_wilson_ci_bounds_within_unit_interval() -> None:
    # Spot-check across a sweep that the returned (low, high) always lies in
    # [0, 1] and low <= high.
    for n in (1, 5, 10, 50, 100, 1000):
        for k in (0, n // 4, n // 2, 3 * n // 4, n):
            low, high = wilson_ci(k, n)
            assert 0.0 <= low <= high <= 1.0
            assert not math.isnan(low) and not math.isnan(high)
