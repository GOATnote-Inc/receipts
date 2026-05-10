"""Tests for L0 deterministic scorer / rule registry (J2).

L0 is the cheap, deterministic, no-LLM layer of the CEIS judge stack. Rules
take a plain input dict and return either ``None`` (no issue) or a single
``Issue``. The default registry is pre-loaded with six built-in rules:
three engineering and three clinical. The registry pattern allows downstream
teams to register additional domain rules without modifying ``l0.py``.

Why this matters: L0 catches the deterministic violations (e.g. allergy in
medication order, epic with zero PRs) before any LLM is invoked, keeping
the dual-judge stop-hook fast and cheap.
"""

from __future__ import annotations

import pytest

from receipts.judge import Issue, RuleRegistry, run_rules

# --------------------------- Issue / dataclass ---------------------------


def test_issue_dataclass_fields() -> None:
    issue = Issue(
        rule_name="x",
        severity="warn",
        message="m",
        target_id=1,
        target_kind="epic",
    )
    assert issue.rule_name == "x"
    assert issue.severity == "warn"
    assert issue.message == "m"
    assert issue.target_id == 1
    assert issue.target_kind == "epic"


# ----------------------------- Registry plumbing -----------------------------


def test_registry_register_new_rule_runs() -> None:
    reg = RuleRegistry()

    def predicate(d: dict) -> Issue | None:
        if d.get("flag"):
            return Issue(
                rule_name="custom",
                severity="info",
                message="flag was set",
                target_id=d["id"],
                target_kind="thing",
            )
        return None

    reg.register("custom_domain", "custom", predicate)
    issues = reg.run("custom_domain", {"id": 7, "flag": True})
    assert len(issues) == 1
    assert issues[0].rule_name == "custom"

    no_issues = reg.run("custom_domain", {"id": 7, "flag": False})
    assert no_issues == []


def test_registry_unknown_domain_raises() -> None:
    reg = RuleRegistry()
    with pytest.raises(ValueError, match="domain"):
        reg.run("does-not-exist", {})


def test_fresh_registry_is_independent_of_default() -> None:
    """A new RuleRegistry instance starts empty -- proves "remove" via reset."""
    reg = RuleRegistry()
    with pytest.raises(ValueError, match="domain"):
        reg.run("eng", {})


# --------------------------- Engineering: epic with zero PR ----------------


def test_eng_epic_with_zero_pr_after_14_days_fires() -> None:
    issues = run_rules(
        "eng",
        {
            "epic_id": 1,
            "epic_external_id": "EPIC-1",
            "created_at": "2026-04-01T00:00:00Z",
            "today": "2026-05-01T00:00:00Z",
            "pr_count": 0,
            "acceptance_criteria": ["x"],  # avoid co-firing
        },
    )
    names = {i.rule_name for i in issues}
    assert "epic-with-zero-pr-after-14-days" in names


def test_eng_epic_with_zero_pr_negative_too_new() -> None:
    issues = run_rules(
        "eng",
        {
            "epic_id": 1,
            "epic_external_id": "EPIC-1",
            "created_at": "2026-04-28T00:00:00Z",
            "today": "2026-05-01T00:00:00Z",
            "pr_count": 0,
            "acceptance_criteria": ["x"],
        },
    )
    names = {i.rule_name for i in issues}
    assert "epic-with-zero-pr-after-14-days" not in names


def test_eng_epic_with_zero_pr_negative_has_prs() -> None:
    issues = run_rules(
        "eng",
        {
            "epic_id": 1,
            "epic_external_id": "EPIC-1",
            "created_at": "2026-04-01T00:00:00Z",
            "today": "2026-05-01T00:00:00Z",
            "pr_count": 3,
            "acceptance_criteria": ["x"],
        },
    )
    names = {i.rule_name for i in issues}
    assert "epic-with-zero-pr-after-14-days" not in names


# --------------------------- Engineering: orphan PR ----------------


def test_eng_orphan_pr_no_epic_fires() -> None:
    issues = run_rules(
        "eng",
        {"pr_id": 42, "epic_external_id": None},
    )
    names = {i.rule_name for i in issues}
    assert "orphan-pr-no-epic" in names
    fired = next(i for i in issues if i.rule_name == "orphan-pr-no-epic")
    assert fired.target_id == 42
    assert fired.target_kind == "pull_request"


def test_eng_orphan_pr_negative_has_epic() -> None:
    issues = run_rules(
        "eng",
        {"pr_id": 42, "epic_external_id": "EPIC-1"},
    )
    names = {i.rule_name for i in issues}
    assert "orphan-pr-no-epic" not in names


# --------------------------- Engineering: missing AC ----------------


def test_eng_missing_acceptance_criteria_fires() -> None:
    issues = run_rules("eng", {"epic_id": 9, "acceptance_criteria": []})
    names = {i.rule_name for i in issues}
    assert "missing-acceptance-criteria" in names


def test_eng_missing_acceptance_criteria_negative() -> None:
    issues = run_rules(
        "eng",
        {"epic_id": 9, "acceptance_criteria": ["user can log in"]},
    )
    names = {i.rule_name for i in issues}
    assert "missing-acceptance-criteria" not in names


# --------------------------- Clinical: allergy-conflict ----------------


def test_clinical_allergy_conflict_fires() -> None:
    issues = run_rules(
        "clinical",
        {
            "patient_allergies": ["penicillin", "sulfa"],
            "ordered_medication": "Penicillin",  # case-insensitive
        },
    )
    names = {i.rule_name for i in issues}
    assert "allergy-conflict" in names
    fired = next(i for i in issues if i.rule_name == "allergy-conflict")
    assert fired.severity == "critical"


def test_clinical_allergy_conflict_negative() -> None:
    issues = run_rules(
        "clinical",
        {
            "patient_allergies": ["penicillin"],
            "ordered_medication": "amoxicillin",
        },
    )
    names = {i.rule_name for i in issues}
    assert "allergy-conflict" not in names


# --------------------------- Clinical: dosage out of range ----------------


def test_clinical_dosage_out_of_range_below_fires() -> None:
    issues = run_rules(
        "clinical",
        {
            "medication": "morphine",
            "dose_mg": 0.5,
            "min_mg": 2.0,
            "max_mg": 10.0,
        },
    )
    names = {i.rule_name for i in issues}
    assert "dosage-out-of-range" in names


def test_clinical_dosage_out_of_range_above_fires() -> None:
    issues = run_rules(
        "clinical",
        {
            "medication": "morphine",
            "dose_mg": 200.0,
            "min_mg": 2.0,
            "max_mg": 10.0,
        },
    )
    names = {i.rule_name for i in issues}
    assert "dosage-out-of-range" in names


def test_clinical_dosage_out_of_range_negative_within_band() -> None:
    issues = run_rules(
        "clinical",
        {
            "medication": "morphine",
            "dose_mg": 4.0,
            "min_mg": 2.0,
            "max_mg": 10.0,
        },
    )
    names = {i.rule_name for i in issues}
    assert "dosage-out-of-range" not in names


# --------------------------- Clinical: missing red flag ----------------


def test_clinical_missing_red_flag_fires() -> None:
    issues = run_rules(
        "clinical",
        {
            "chief_complaint": "chest pain",
            "red_flags_documented": ["radiation"],
            "required_red_flags": ["radiation", "diaphoresis", "dyspnea"],
        },
    )
    names = {i.rule_name for i in issues}
    assert "missing-red-flag" in names


def test_clinical_missing_red_flag_negative_all_present() -> None:
    issues = run_rules(
        "clinical",
        {
            "chief_complaint": "chest pain",
            "red_flags_documented": ["radiation", "diaphoresis", "dyspnea"],
            "required_red_flags": ["radiation", "diaphoresis", "dyspnea"],
        },
    )
    names = {i.rule_name for i in issues}
    assert "missing-red-flag" not in names


# --------------------------- Multi-issue ----------------


def test_multi_issue_clinical_two_rules_fire() -> None:
    issues = run_rules(
        "clinical",
        {
            "patient_allergies": ["morphine"],
            "ordered_medication": "morphine",
            "medication": "morphine",
            "dose_mg": 200.0,
            "min_mg": 2.0,
            "max_mg": 10.0,
            # required_red_flags absent => missing-red-flag won't fire
        },
    )
    names = {i.rule_name for i in issues}
    assert "allergy-conflict" in names
    assert "dosage-out-of-range" in names
    assert len(issues) == 2


def test_multi_issue_eng_two_rules_fire() -> None:
    issues = run_rules(
        "eng",
        {
            "epic_id": 1,
            "epic_external_id": "EPIC-1",
            "created_at": "2026-04-01T00:00:00Z",
            "today": "2026-05-01T00:00:00Z",
            "pr_count": 0,
            "acceptance_criteria": [],
        },
    )
    names = {i.rule_name for i in issues}
    assert "epic-with-zero-pr-after-14-days" in names
    assert "missing-acceptance-criteria" in names
