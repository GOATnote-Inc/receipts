"""Tests for L1 structural scorer (J3).

L1 is the schema-presence + regex-coverage layer of the CEIS judge stack. It
sits between L0 (deterministic rules) and L2 (LLM judge): it asks "did the
upstream artefact include the structural fields we require, and do the values
that ARE present match the shape we expected?".

``score_structure`` is pure -- no I/O, stdlib-only -- so it runs on every
event in the run log alongside L0 without budget concern.
"""

from __future__ import annotations

from receipts.judge import StructuralResult, score_structure

# --------------------------- dataclass shape ---------------------------


def test_structural_result_dataclass_fields() -> None:
    result = StructuralResult(
        required_present=["patient.name"],
        required_missing=["patient.allergies"],
        coverage_score=0.5,
        regex_matches={"note.assessment": 2},
    )
    assert result.required_present == ["patient.name"]
    assert result.required_missing == ["patient.allergies"]
    assert result.coverage_score == 0.5
    assert result.regex_matches == {"note.assessment": 2}


# --------------------------- coverage scoring ---------------------------


def test_all_required_present() -> None:
    """All required dotted paths resolve to non-empty values -> coverage 1.0."""
    payload = {
        "patient": {"name": "Jane Doe", "allergies": ["penicillin"]},
        "note": {"assessment": "Pneumonia, community acquired."},
    }
    result = score_structure(
        payload,
        required_fields=["patient.name", "patient.allergies", "note.assessment"],
    )
    assert result.coverage_score == 1.0
    assert set(result.required_present) == {
        "patient.name",
        "patient.allergies",
        "note.assessment",
    }
    assert result.required_missing == []
    assert result.regex_matches == {}


def test_some_missing() -> None:
    """Partial presence -> coverage strictly between 0 and 1, missing list correct."""
    payload = {
        "patient": {"name": "Jane Doe"},
        "note": {"assessment": "Asthma exacerbation."},
        # patient.allergies absent
    }
    result = score_structure(
        payload,
        required_fields=["patient.name", "patient.allergies", "note.assessment"],
    )
    assert 0.0 < result.coverage_score < 1.0
    # 2 of 3 present
    assert result.coverage_score == 2 / 3
    assert set(result.required_present) == {"patient.name", "note.assessment"}
    assert result.required_missing == ["patient.allergies"]


def test_none_present() -> None:
    """No required fields resolve -> coverage 0.0 and full missing list."""
    payload = {"unrelated": "value"}
    result = score_structure(
        payload,
        required_fields=["patient.name", "note.assessment"],
    )
    assert result.coverage_score == 0.0
    assert result.required_present == []
    assert set(result.required_missing) == {"patient.name", "note.assessment"}


# --------------------------- empty-required handling ---------------------------


def test_empty_required_fields_returns_one() -> None:
    """Zero required fields -> coverage 1.0 (vacuously satisfied, no /0)."""
    result = score_structure({"anything": 1}, required_fields=[])
    assert result.coverage_score == 1.0
    assert result.required_present == []
    assert result.required_missing == []


# --------------------------- dotted-path resolution ---------------------------


def test_dotted_path_resolution() -> None:
    """Deeply nested dotted paths resolve through multiple dict layers."""
    payload = {
        "patient": {
            "demographics": {
                "name": {"first": "Ada", "last": "Lovelace"},
            },
            "allergies": [],  # empty -> treated as missing
        },
        "note": {"assessment": None},  # None -> treated as missing
    }
    result = score_structure(
        payload,
        required_fields=[
            "patient.demographics.name.first",
            "patient.demographics.name.last",
            "patient.allergies",
            "note.assessment",
        ],
    )
    assert set(result.required_present) == {
        "patient.demographics.name.first",
        "patient.demographics.name.last",
    }
    assert set(result.required_missing) == {"patient.allergies", "note.assessment"}
    assert result.coverage_score == 0.5


def test_dotted_path_through_non_dict_is_missing() -> None:
    """Traversal that hits a non-dict before terminus -> treated as missing, no exception."""
    payload = {"patient": "string-not-dict"}
    result = score_structure(
        payload,
        required_fields=["patient.name"],
    )
    assert result.required_missing == ["patient.name"]
    assert result.required_present == []
    assert result.coverage_score == 0.0


# --------------------------- regex match counting ---------------------------


def test_regex_pattern_match_count() -> None:
    """regex_patterns counts ``re.findall`` matches per dotted path."""
    payload = {
        "note": {
            "assessment": "BP 120/80, HR 88, RR 16, SpO2 98%.",
            "plan": "Continue lisinopril 10 mg daily.",
        },
    }
    result = score_structure(
        payload,
        required_fields=["note.assessment", "note.plan"],
        regex_patterns={
            "note.assessment": r"\d+",  # 120, 80, 88, 16, 98, 2 -> 6 matches
            "note.plan": r"\bmg\b",  # 1 match
        },
    )
    assert result.coverage_score == 1.0
    assert result.regex_matches["note.assessment"] == 6
    assert result.regex_matches["note.plan"] == 1


def test_regex_on_missing_path_is_zero() -> None:
    """Regex against an unresolvable dotted path counts zero rather than erroring."""
    payload = {"note": {"assessment": "ok"}}
    result = score_structure(
        payload,
        required_fields=["note.assessment"],
        regex_patterns={"note.absent": r"."},
    )
    assert result.regex_matches["note.absent"] == 0


def test_regex_on_non_string_value_is_zero() -> None:
    """Regex against a non-string resolved value counts zero (no coercion surprises)."""
    payload = {"counts": {"value": 5}}
    result = score_structure(
        payload,
        required_fields=["counts.value"],
        regex_patterns={"counts.value": r"\d+"},
    )
    assert result.regex_matches["counts.value"] == 0


def test_no_regex_patterns_returns_empty_dict() -> None:
    """Omitting regex_patterns -> regex_matches is an empty dict, not None."""
    result = score_structure({"x": 1}, required_fields=["x"])
    assert result.regex_matches == {}
