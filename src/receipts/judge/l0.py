"""L0 deterministic scorer for the CEIS judge stack.

L0 is the cheap, no-LLM, no-network layer of the dual-judge pipeline. Rules are
pure functions over a plain ``dict`` input. Each rule returns either ``None``
(no issue) or a single ``Issue`` describing the violation. Rules are organised
by ``domain`` ("eng" / "clinical") so the same registry can serve both
products (Engineering Receipts and Clinical Audit Ledger) without coupling.

Design notes
------------
- ``Issue`` is a ``@dataclass(frozen=True)`` rather than a Pydantic model.
  L0 runs on every event in the run log; instantiation overhead matters.
- The registry is registered ad-hoc rather than via decorators so the built-in
  rules and any downstream registrations sit on the same code path.
- The module exposes a pre-populated default ``RuleRegistry`` and a convenience
  ``run_rules`` wrapper. Tests construct fresh ``RuleRegistry()`` instances
  when they need isolation.
- Rule predicates that need fields not present in ``input_dict`` return
  ``None`` silently. This lets the registry's ``run`` walk all rules in a
  domain with a single payload that only partially covers them, which is the
  expected pattern when several rules co-fire on the same upstream event.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Severity = Literal["info", "warn", "critical"]

# A rule predicate maps an input dict to either an Issue or None.
RulePredicate = Callable[[dict], "Issue | None"]


@dataclass(frozen=True)
class Issue:
    """A single L0 rule violation.

    ``severity`` is constrained to {"info", "warn", "critical"} via Literal so
    downstream gates (the κ stop-hook, the merkle ledger writer) can rely on a
    closed enumeration without a runtime check.
    """

    rule_name: str
    severity: Severity
    message: str
    target_id: int
    target_kind: str


class RuleRegistry:
    """Domain -> {rule_name -> predicate} mapping.

    The registry is intentionally minimal: register predicates, then call
    ``run`` for a given domain with an input dict. The registry does not
    deduplicate rules by name -- ``register`` overwrites an existing rule
    under the same (domain, name) key. That keeps the contract simple for
    tests that want to swap a rule in place.
    """

    def __init__(self) -> None:
        self._rules: dict[str, dict[str, RulePredicate]] = {}

    def register(
        self,
        domain: str,
        name: str,
        predicate: RulePredicate,
    ) -> None:
        self._rules.setdefault(domain, {})[name] = predicate

    def run(self, domain: str, input_dict: dict) -> list[Issue]:
        if domain not in self._rules:
            raise ValueError(f"RuleRegistry.run: unknown domain {domain!r}")
        issues: list[Issue] = []
        for predicate in self._rules[domain].values():
            result = predicate(input_dict)
            if result is not None:
                issues.append(result)
        return issues


# --------------------------- helpers ---------------------------


def _parse_iso8601(value: str) -> datetime:
    """Parse an ISO-8601 timestamp. Accepts trailing 'Z' for UTC.

    Python 3.12+ accepts 'Z' natively in ``fromisoformat`` but we normalise
    defensively so the rule stays portable across runners.
    """

    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


# --------------------------- engineering rules ---------------------------


def _eng_epic_with_zero_pr_after_14_days(d: dict) -> Issue | None:
    """Epic created >= 14 days ago with zero linked PRs.

    Required fields: ``epic_id``, ``created_at``, ``today``, ``pr_count``.
    Optional: ``epic_external_id`` (surfaced in the message when present).
    """

    if "epic_id" not in d or "created_at" not in d or "today" not in d:
        return None
    if "pr_count" not in d:
        return None
    if d["pr_count"] != 0:
        return None

    try:
        created = _parse_iso8601(d["created_at"])
        today = _parse_iso8601(d["today"])
    except (TypeError, ValueError):
        return None

    if (today - created).days < 14:
        return None

    ext = d.get("epic_external_id") or f"epic#{d['epic_id']}"
    return Issue(
        rule_name="epic-with-zero-pr-after-14-days",
        severity="warn",
        message=f"Epic {ext} has zero PRs after {(today - created).days} days",
        target_id=int(d["epic_id"]),
        target_kind="epic",
    )


def _eng_orphan_pr_no_epic(d: dict) -> Issue | None:
    """Pull request not linked to any epic (epic_external_id is None)."""

    if "pr_id" not in d or "epic_external_id" not in d:
        return None
    if d["epic_external_id"] is not None:
        return None
    return Issue(
        rule_name="orphan-pr-no-epic",
        severity="warn",
        message=f"PR #{d['pr_id']} has no linked epic",
        target_id=int(d["pr_id"]),
        target_kind="pull_request",
    )


def _eng_missing_acceptance_criteria(d: dict) -> Issue | None:
    """Epic with no acceptance criteria recorded.

    ``acceptance_criteria`` is treated as missing if absent OR falsy (empty
    list / None). An epic that legitimately has criteria but they were
    truncated upstream still surfaces here, which is the desired conservative
    behaviour for a stop-hook gate.
    """

    if "epic_id" not in d or "acceptance_criteria" not in d:
        return None
    if d["acceptance_criteria"]:
        return None
    return Issue(
        rule_name="missing-acceptance-criteria",
        severity="warn",
        message=f"Epic#{d['epic_id']} has no acceptance criteria",
        target_id=int(d["epic_id"]),
        target_kind="epic",
    )


# --------------------------- clinical rules ---------------------------


def _clinical_allergy_conflict(d: dict) -> Issue | None:
    """Ordered medication appears on the patient's allergy list (case-insensitive)."""

    if "patient_allergies" not in d or "ordered_medication" not in d:
        return None
    med = str(d["ordered_medication"]).strip().lower()
    allergies = {str(a).strip().lower() for a in d["patient_allergies"]}
    if med not in allergies:
        return None
    return Issue(
        rule_name="allergy-conflict",
        severity="critical",
        message=f"Ordered medication {d['ordered_medication']!r} conflicts with documented allergy",
        target_id=int(d.get("order_id", 0)),
        target_kind="medication_order",
    )


def _clinical_dosage_out_of_range(d: dict) -> Issue | None:
    """Ordered dose is outside the [min_mg, max_mg] safety band."""

    required = ("medication", "dose_mg", "min_mg", "max_mg")
    if any(k not in d for k in required):
        return None
    dose = float(d["dose_mg"])
    if d["min_mg"] <= dose <= d["max_mg"]:
        return None
    return Issue(
        rule_name="dosage-out-of-range",
        severity="critical",
        message=(
            f"Dose {dose} mg of {d['medication']} outside safe band [{d['min_mg']}, {d['max_mg']}]"
        ),
        target_id=int(d.get("order_id", 0)),
        target_kind="medication_order",
    )


def _clinical_missing_red_flag(d: dict) -> Issue | None:
    """Required red-flag elements for a chief complaint not all documented."""

    required = ("chief_complaint", "red_flags_documented", "required_red_flags")
    if any(k not in d for k in required):
        return None
    documented = {str(x).strip().lower() for x in d["red_flags_documented"]}
    needed = [str(x).strip().lower() for x in d["required_red_flags"]]
    missing = [x for x in needed if x not in documented]
    if not missing:
        return None
    return Issue(
        rule_name="missing-red-flag",
        severity="warn",
        message=(f"Chief complaint {d['chief_complaint']!r} missing red-flag elements: {missing}"),
        target_id=int(d.get("encounter_id", 0)),
        target_kind="encounter",
    )


# --------------------------- default registry ---------------------------

_DEFAULT_REGISTRY = RuleRegistry()
_DEFAULT_REGISTRY.register(
    "eng",
    "epic-with-zero-pr-after-14-days",
    _eng_epic_with_zero_pr_after_14_days,
)
_DEFAULT_REGISTRY.register("eng", "orphan-pr-no-epic", _eng_orphan_pr_no_epic)
_DEFAULT_REGISTRY.register(
    "eng",
    "missing-acceptance-criteria",
    _eng_missing_acceptance_criteria,
)
_DEFAULT_REGISTRY.register("clinical", "allergy-conflict", _clinical_allergy_conflict)
_DEFAULT_REGISTRY.register("clinical", "dosage-out-of-range", _clinical_dosage_out_of_range)
_DEFAULT_REGISTRY.register("clinical", "missing-red-flag", _clinical_missing_red_flag)


def run_rules(domain: str, input_dict: dict) -> list[Issue]:
    """Convenience wrapper that calls the module-level default registry."""

    return _DEFAULT_REGISTRY.run(domain, input_dict)


__all__ = ["Issue", "RuleRegistry", "run_rules"]
