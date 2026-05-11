"""L6: regulatory export generators.

Four output formats for a populated ledger state:

- Markdown — human-readable VP-Eng / CMIO digest (string-templated, no Jinja)
- CSV — flat tabular row per epic via stdlib `csv.writer` into StringIO
- SARIF v2.1.0 JSON — drift findings as static-analysis results
- FHIR R4 Bundle JSON — Composition resources for clinical-audit consumers

Determinism contract:
- All outputs MUST be byte-stable across repeated calls against the same DB.
- Epic ordering is `external_id` ASC.
- Drift, PR, meeting, thread ordering follows `LineageQuery` (id ASC) which
  is itself stable.
- JSON is emitted with `sort_keys=True, separators=(",", ":")` so dict-key
  ordering is fixed regardless of insertion order.

Citations contract:
- Every output references the epic `external_id`.
- When PRs exist on the epic, at least one PR `external_id` appears.
- Markdown additionally cites meeting + thread external_ids when present.
- SARIF locations carry the epic `external_id` in `physicalLocation.artifactLocation.uri`.
- FHIR Composition sections carry the PR/meeting/thread external_ids as
  `Reference.identifier` values inside `section.entry`.

stdlib only. No new deps.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from receipts.ledger.models import DriftScore, Epic
from receipts.ledger.queries import LineageGraph, LineageQuery

_TOOL_NAME = "receipts"
_TOOL_VERSION = "0.1.0"
_SARIF_SCHEMA = (
    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"
)
_FHIR_SYSTEM = "https://receipts.thegoatnote.com/fhir"


def _resolve_epics(session: Session, epic_external_ids: list[str] | None) -> list[Epic]:
    """Return Epic rows ordered by external_id ASC, filtered if list provided."""
    stmt = select(Epic).order_by(Epic.external_id.asc())
    if epic_external_ids is not None:
        stmt = stmt.where(Epic.external_id.in_(list(epic_external_ids)))
    return list(session.execute(stmt).scalars().all())


def _drift_for_epic(session: Session, epic_id: int) -> list[DriftScore]:
    """Return drift rows for an epic in deterministic (layer, id) order."""
    stmt = (
        select(DriftScore)
        .where(DriftScore.epic_id == epic_id)
        .order_by(DriftScore.layer.asc(), DriftScore.id.asc())
    )
    return list(session.execute(stmt).scalars().all())


def _format_criteria(criteria: Any) -> list[str]:
    """Render acceptance_criteria JSON into a flat list of bullet strings.

    Accepts: dict[str, list[str]] (e.g. `{"must": [...]}`), list[str], or str.
    Anything else is `str(value)`-ed.
    """
    lines: list[str] = []
    if isinstance(criteria, dict):
        for key in sorted(criteria.keys()):
            val = criteria[key]
            if isinstance(val, list):
                for item in val:
                    lines.append(f"- [{key}] {item}")
            else:
                lines.append(f"- [{key}] {val}")
    elif isinstance(criteria, list):
        for item in criteria:
            lines.append(f"- {item}")
    elif criteria:
        lines.append(f"- {criteria}")
    return lines


def _drift_level(score: float) -> str:
    """Map a 0..1 drift score to a SARIF level."""
    if score >= 0.5:
        return "error"
    if score >= 0.2:
        return "warning"
    return "note"


def _ci_str(low: float | None, high: float | None) -> str:
    if low is None or high is None:
        return ""
    return f" [CI {low:.2f}-{high:.2f}]"


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def generate_markdown(session: Session, epic_external_ids: list[str] | None = None) -> str:
    """Render a Markdown digest of the ledger state.

    Structure:
        # Receipts Ledger Export

        ## Epic <external_id> - <title>

        ### Acceptance criteria
        - [must] ...

        ### Pull requests
        - <pr.external_id> - <pr.title>

        ### Meetings
        - <meeting.external_id> - <meeting.title>

        ### Threads
        - <thread.external_id> - <thread.channel>

        ### Drift scores
        - <layer> <score>[CI low-high] (judge_run_id: <run-id>)
    """
    epics = _resolve_epics(session, epic_external_ids)
    q = LineageQuery(session)

    parts: list[str] = ["# Receipts Ledger Export", ""]
    parts.append(f"_tool: {_TOOL_NAME} {_TOOL_VERSION}_")
    parts.append("")

    for epic in epics:
        parts.append(f"## Epic {epic.external_id} - {epic.title}")
        parts.append("")

        crit_lines = _format_criteria(epic.acceptance_criteria)
        parts.append("### Acceptance criteria")
        if crit_lines:
            parts.extend(crit_lines)
        else:
            parts.append("- (none)")
        parts.append("")

        graph: LineageGraph = q.lineage_graph(epic.external_id)

        parts.append("### Pull requests")
        if graph["prs"]:
            for pr in graph["prs"]:
                parts.append(f"- {pr.external_id} - {pr.title}")
        else:
            parts.append("- (none)")
        parts.append("")

        parts.append("### Meetings")
        if graph["meetings"]:
            for m in graph["meetings"]:
                parts.append(f"- {m.external_id} - {m.title}")
        else:
            parts.append("- (none)")
        parts.append("")

        parts.append("### Threads")
        if graph["threads"]:
            for t in graph["threads"]:
                parts.append(f"- {t.external_id} - {t.channel}")
        else:
            parts.append("- (none)")
        parts.append("")

        parts.append("### Drift scores")
        drifts = _drift_for_epic(session, epic.id)
        if drifts:
            for d in drifts:
                run = d.judge_run_id or "n/a"
                parts.append(
                    f"- {d.layer} {d.score:.3f}{_ci_str(d.ci_low, d.ci_high)} (judge_run_id: {run})"
                )
        else:
            parts.append("- (none)")
        parts.append("")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


_CSV_HEADER = [
    "epic_external_id",
    "epic_title",
    "pr_external_ids",
    "meeting_external_ids",
    "thread_external_ids",
    "drift_layers",
    "drift_scores",
]


def generate_csv(session: Session, epic_external_ids: list[str] | None = None) -> str:
    """Render a flat CSV digest, one row per epic.

    Multi-valued fields (PR / meeting / thread external_ids, drift layers and
    scores) are joined with `|` so the cell remains a single string.
    """
    epics = _resolve_epics(session, epic_external_ids)
    q = LineageQuery(session)

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_CSV_HEADER)

    for epic in epics:
        graph: LineageGraph = q.lineage_graph(epic.external_id)
        prs = "|".join(p.external_id for p in graph["prs"])
        meetings = "|".join(m.external_id for m in graph["meetings"])
        threads = "|".join(t.external_id for t in graph["threads"])
        drifts = _drift_for_epic(session, epic.id)
        layers = "|".join(d.layer for d in drifts)
        scores = "|".join(f"{d.score:.3f}" for d in drifts)
        writer.writerow([epic.external_id, epic.title, prs, meetings, threads, layers, scores])

    return buf.getvalue()


# ---------------------------------------------------------------------------
# SARIF v2.1.0
# ---------------------------------------------------------------------------


def _sarif_result(epic: Epic, drift: DriftScore, pr_ids: list[str]) -> dict[str, Any]:
    rule_id = f"receipts.drift.{drift.layer}"
    text = (
        f"Epic {epic.external_id} drift score {drift.score:.3f} at layer {drift.layer}"
        f"{_ci_str(drift.ci_low, drift.ci_high)}"
    )
    location: dict[str, Any] = {
        "physicalLocation": {
            "artifactLocation": {
                "uri": f"epic/{epic.external_id}",
            }
        },
        "logicalLocations": [
            {"name": epic.external_id, "kind": "object"},
        ],
    }
    properties: dict[str, Any] = {
        "epic_external_id": epic.external_id,
        "layer": drift.layer,
        "score": drift.score,
    }
    if pr_ids:
        properties["pr_external_ids"] = list(pr_ids)
    if drift.judge_run_id is not None:
        properties["judge_run_id"] = drift.judge_run_id
    return {
        "ruleId": rule_id,
        "level": _drift_level(drift.score),
        "message": {"text": text},
        "locations": [location],
        "properties": properties,
    }


def generate_sarif(session: Session, epic_external_ids: list[str] | None = None) -> str:
    """Render a SARIF v2.1.0 JSON document.

    One run, one result per drift_score row. Each result carries the epic
    external_id in `locations[].physicalLocation.artifactLocation.uri` and the
    associated PR external_ids in `properties.pr_external_ids` so static-analysis
    consumers (GitHub code scanning, Defender, etc.) can attribute findings.
    """
    epics = _resolve_epics(session, epic_external_ids)
    q = LineageQuery(session)

    rules_seen: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []

    for epic in epics:
        graph: LineageGraph = q.lineage_graph(epic.external_id)
        pr_ids = [p.external_id for p in graph["prs"]]
        for drift in _drift_for_epic(session, epic.id):
            rule_id = f"receipts.drift.{drift.layer}"
            if rule_id not in rules_seen:
                rules_seen[rule_id] = {
                    "id": rule_id,
                    "name": f"DriftLayer{drift.layer.upper()}",
                    "shortDescription": {"text": f"Drift score for layer {drift.layer}"},
                    "fullDescription": {
                        "text": (
                            f"CEIS {drift.layer} layer drift score for an epic, "
                            "produced by the receipts judge stack."
                        )
                    },
                    "defaultConfiguration": {"level": _drift_level(drift.score)},
                }
            results.append(_sarif_result(epic, drift, pr_ids))

    rules = sorted(rules_seen.values(), key=lambda r: r["id"])

    doc: dict[str, Any] = {
        "$schema": _SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": _TOOL_NAME,
                        "version": _TOOL_VERSION,
                        "informationUri": "https://github.com/GOATnote-Inc/receipts",
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# FHIR R4 Bundle
# ---------------------------------------------------------------------------


def _fhir_identifier(value: str, system_suffix: str) -> dict[str, Any]:
    return {
        "system": f"{_FHIR_SYSTEM}/{system_suffix}",
        "value": value,
    }


def _fhir_drift_extension(drift: DriftScore) -> dict[str, Any]:
    ext_parts: list[dict[str, Any]] = [
        {"url": "layer", "valueString": drift.layer},
        {"url": "score", "valueDecimal": drift.score},
    ]
    if drift.ci_low is not None:
        ext_parts.append({"url": "ciLow", "valueDecimal": drift.ci_low})
    if drift.ci_high is not None:
        ext_parts.append({"url": "ciHigh", "valueDecimal": drift.ci_high})
    if drift.judge_run_id is not None:
        ext_parts.append({"url": "judgeRunId", "valueString": drift.judge_run_id})
    return {
        "url": f"{_FHIR_SYSTEM}/StructureDefinition/drift-finding",
        "extension": ext_parts,
    }


def _fhir_composition(session: Session, epic: Epic, q: LineageQuery) -> dict[str, Any]:
    graph: LineageGraph = q.lineage_graph(epic.external_id)
    drifts = _drift_for_epic(session, epic.id)

    # subject = epic (modeled as a Group resource reference via identifier).
    subject = {
        "type": "Group",
        "identifier": _fhir_identifier(epic.external_id, "epic"),
        "display": epic.title,
    }

    # Section: Acceptance criteria.
    criteria_lines = _format_criteria(epic.acceptance_criteria)
    criteria_html = (
        '<div xmlns="http://www.w3.org/1999/xhtml"><ul>'
        + "".join(f"<li>{line[2:]}</li>" for line in criteria_lines)
        + "</ul></div>"
    )
    criteria_section: dict[str, Any] = {
        "title": "Acceptance criteria",
        "code": {
            "coding": [
                {
                    "system": f"{_FHIR_SYSTEM}/CodeSystem/section",
                    "code": "acceptance-criteria",
                    "display": "Acceptance criteria",
                }
            ]
        },
        "text": {"status": "additional", "div": criteria_html},
    }

    # Section: Pull requests (cite by external_id).
    pr_entries = [
        {
            "type": "Task",
            "identifier": _fhir_identifier(p.external_id, "pr"),
            "display": p.title,
        }
        for p in graph["prs"]
    ]
    pr_section: dict[str, Any] = {
        "title": "Pull requests",
        "code": {
            "coding": [
                {
                    "system": f"{_FHIR_SYSTEM}/CodeSystem/section",
                    "code": "pull-requests",
                    "display": "Pull requests",
                }
            ]
        },
        "entry": pr_entries,
    }

    # Section: Meetings.
    meeting_entries = [
        {
            "type": "Encounter",
            "identifier": _fhir_identifier(m.external_id, "meeting"),
            "display": m.title,
        }
        for m in graph["meetings"]
    ]
    meeting_section: dict[str, Any] = {
        "title": "Meetings",
        "code": {
            "coding": [
                {
                    "system": f"{_FHIR_SYSTEM}/CodeSystem/section",
                    "code": "meetings",
                    "display": "Meetings",
                }
            ]
        },
        "entry": meeting_entries,
    }

    # Section: Threads.
    thread_entries = [
        {
            "type": "Communication",
            "identifier": _fhir_identifier(t.external_id, "thread"),
            "display": t.channel,
        }
        for t in graph["threads"]
    ]
    thread_section: dict[str, Any] = {
        "title": "Threads",
        "code": {
            "coding": [
                {
                    "system": f"{_FHIR_SYSTEM}/CodeSystem/section",
                    "code": "threads",
                    "display": "Threads",
                }
            ]
        },
        "entry": thread_entries,
    }

    # Section: Drift findings as extensions.
    drift_section: dict[str, Any] = {
        "title": "Drift findings",
        "code": {
            "coding": [
                {
                    "system": f"{_FHIR_SYSTEM}/CodeSystem/section",
                    "code": "drift-findings",
                    "display": "Drift findings",
                }
            ]
        },
        "extension": [_fhir_drift_extension(d) for d in drifts],
    }

    composition: dict[str, Any] = {
        "resourceType": "Composition",
        "identifier": _fhir_identifier(epic.external_id, "composition"),
        "status": "final",
        "type": {
            "coding": [
                {
                    "system": f"{_FHIR_SYSTEM}/CodeSystem/composition-type",
                    "code": "epic-attestation",
                    "display": "Epic intent-vs-execution attestation",
                }
            ]
        },
        "subject": subject,
        "title": f"Epic {epic.external_id} - {epic.title}",
        "section": [
            criteria_section,
            pr_section,
            meeting_section,
            thread_section,
            drift_section,
        ],
    }
    return composition


def generate_fhir_bundle(session: Session, epic_external_ids: list[str] | None = None) -> str:
    """Render a FHIR R4 Bundle (collection) of Composition resources.

    One Composition per epic; sections cover acceptance criteria, PRs, meetings,
    threads, and drift findings (as extensions). All external_ids are carried as
    Reference identifiers so clinical-audit consumers can resolve back to source
    artifacts.
    """
    epics = _resolve_epics(session, epic_external_ids)
    q = LineageQuery(session)

    entries: list[dict[str, Any]] = []
    for epic in epics:
        composition = _fhir_composition(session, epic, q)
        entries.append(
            {
                "fullUrl": f"urn:uuid:epic-{epic.external_id}",
                "resource": composition,
            }
        )

    bundle: dict[str, Any] = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": entries,
    }
    return json.dumps(bundle, sort_keys=True, separators=(",", ":"))


__all__ = [
    "generate_csv",
    "generate_fhir_bundle",
    "generate_markdown",
    "generate_sarif",
]
