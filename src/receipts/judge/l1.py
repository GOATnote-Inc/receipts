"""L1 structural scorer for the CEIS judge stack.

L1 is the schema-presence + regex-coverage layer of the dual-judge pipeline.
Unlike L0, which encodes domain rules as discrete predicates, L1 takes a
declarative "shape" of an artefact -- a list of required dotted-path fields
and (optionally) a per-field regex -- and reports:

- which required paths resolved to a non-None, non-empty value,
- which required paths were absent,
- the coverage_score (fraction of required paths present),
- how many regex matches each pattern produced against its resolved string.

This is a port of lostbench's CEIS layer-1 pattern: cheap, deterministic,
schema-aware checks that gate the more expensive LLM judge (L2). It runs
per-event in the run log alongside L0.

Design notes
------------
- Stdlib only (``re``, ``dataclasses``). No pydantic, no jsonschema -- the
  required-fields contract is light enough that a dotted-path resolver and
  a "not None and truthy" check is the right primitive.
- Empty containers (``""``, ``[]``, ``{}``) are treated as missing. The L1
  contract is "did the upstream tool actually fill this field with content";
  an empty list of allergies is structurally identical to no list at all
  from a downstream-grading standpoint.
- Dotted-path traversal through a non-dict intermediate (e.g. asking for
  ``patient.name`` when ``patient`` is a string) is treated as missing
  rather than raising. L1 is a structural diagnostic, not a typed schema --
  the rule is "this field was not present in the expected form".
- Regex counting uses ``re.findall`` and only operates on resolved values
  that are strings. A pattern targeting a missing path or a non-string
  value records ``0`` rather than raising or coercing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Sentinel for "dotted path did not resolve". Distinct from ``None`` because
# a path may legitimately resolve to a ``None`` value, which we still treat
# as missing -- the sentinel lets the resolver report "structurally absent"
# separately from "present but None" for future extension. Today both map
# to the same "missing" outcome.
_MISSING = object()


@dataclass(frozen=True)
class StructuralResult:
    """L1 structural-completeness result for a single input artefact.

    Attributes:
        required_present: Dotted paths whose values resolved to non-None and
            truthy (non-empty) values. Order matches the order of ``required_fields``
            passed to ``score_structure``.
        required_missing: Dotted paths that were absent, None, or empty.
            Order matches the order of ``required_fields``.
        coverage_score: ``len(required_present) / len(required_fields)``,
            or ``1.0`` when ``required_fields`` is empty (vacuously satisfied).
        regex_matches: Per-pattern match counts from ``re.findall``. Keyed by
            the dotted path the pattern was registered against. A pattern
            against a missing path or non-string value records ``0``.
    """

    required_present: list[str]
    required_missing: list[str]
    coverage_score: float
    regex_matches: dict[str, int] = field(default_factory=dict)


def _resolve_dotted_path(payload: dict, path: str) -> Any:
    """Walk a dotted path through nested dicts.

    Returns the value at the terminus, or ``_MISSING`` if any segment of the
    path is absent or traverses a non-dict. Does not raise on shape errors;
    L1 reports them as missing instead so downstream gates see a single
    "not present" outcome rather than an exception.
    """

    current: Any = payload
    for segment in path.split("."):
        if not isinstance(current, dict):
            return _MISSING
        if segment not in current:
            return _MISSING
        current = current[segment]
    return current


def _is_present(value: Any) -> bool:
    """A field counts as present iff it is non-None and truthy.

    ``False`` and ``0`` are intentionally treated as missing here -- L1 asks
    "did the upstream tool record something meaningful in this slot", and a
    boolean flag or integer that has not been set differs from one that has
    been explicitly set to a non-default value via a separate Pydantic /
    typed-payload check, not via L1. If a downstream caller needs the
    "explicitly zero" semantics, they should encode it as a string ("0") or
    structured object at the producer.
    """

    if value is _MISSING:
        return False
    if value is None:
        return False
    # Treat empty containers / strings as missing.
    if isinstance(value, str | list | tuple | set | dict) and len(value) == 0:
        return False
    # Bool/int/float "zero-likes" are treated as missing for L1's purposes.
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    return True


def score_structure(
    input_dict: dict,
    required_fields: list[str],
    regex_patterns: dict[str, str] | None = None,
) -> StructuralResult:
    """Score the structural completeness of ``input_dict``.

    Args:
        input_dict: The artefact to score. Typically the JSON-decoded body
            of an upstream tool call or LLM response.
        required_fields: Dotted-path names that must resolve to a non-None,
            non-empty value (e.g. ``"patient.allergies"``, ``"note.assessment"``).
            Order is preserved in ``required_present`` / ``required_missing``.
        regex_patterns: Optional mapping of dotted path -> regex source. Each
            pattern is compiled and its ``re.findall`` match count against the
            resolved string value is recorded in ``regex_matches``. A pattern
            whose path does not resolve to a string records ``0``.

    Returns:
        ``StructuralResult`` with the four reporting fields populated.

    Notes:
        - When ``required_fields`` is empty, ``coverage_score`` is ``1.0``.
          This is the vacuous-truth convention shared with κ's degenerate
          case in ``kappa.py`` and is what downstream gates expect.
        - Regex compilation errors propagate as ``re.error`` -- a bad pattern
          is a programmer mistake at the gate config, not a data error, and
          should fail loud.
    """

    patterns = regex_patterns or {}

    present: list[str] = []
    missing: list[str] = []
    for path in required_fields:
        value = _resolve_dotted_path(input_dict, path)
        if _is_present(value):
            present.append(path)
        else:
            missing.append(path)

    coverage = len(present) / len(required_fields) if required_fields else 1.0

    regex_matches: dict[str, int] = {}
    for path, pattern in patterns.items():
        value = _resolve_dotted_path(input_dict, path)
        if not isinstance(value, str):
            regex_matches[path] = 0
            continue
        regex_matches[path] = len(re.findall(pattern, value))

    return StructuralResult(
        required_present=present,
        required_missing=missing,
        coverage_score=coverage,
        regex_matches=regex_matches,
    )


__all__ = ["StructuralResult", "score_structure"]
