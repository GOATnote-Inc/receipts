"""L6 regulatory export generator tests.

Generates 4 export formats (Markdown, CSV, SARIF v2.1.0 JSON, FHIR R4 Bundle
JSON) over a populated ledger state via `LineageQuery`. Each format must be
byte-stable across repeated runs and cite the source artifacts.

Setup pattern mirrors tests/test_ledger_queries.py + tests/test_ledger_merkle.py:
SQLite file in tmpdir, `alembic upgrade head`, then hand-inserted fixture rows.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from receipts.ledger.exports import (
    generate_csv,
    generate_fhir_bundle,
    generate_markdown,
    generate_sarif,
)
from receipts.ledger.models import PR, DriftScore, Edge, Epic, Meeting, Thread

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'receipts.db'}"


@pytest.fixture
def upgraded_engine(db_url: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(cfg, "head")
    engine = create_engine(db_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session(upgraded_engine) -> Session:
    SessionFactory = sessionmaker(bind=upgraded_engine, expire_on_commit=False)
    with SessionFactory() as s:
        yield s


def _seed_exports(s: Session) -> dict:
    """Insert 2 epics with full lineage + drift scores for export tests."""
    t0 = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)

    epic_a = Epic(
        external_id="LIN-A",
        title="Epic A title",
        acceptance_criteria={"must": ["a1", "a2"], "should": ["a3"]},
        created_at=t0,
        updated_at=t0,
    )
    epic_b = Epic(
        external_id="LIN-B",
        title="Epic B title",
        acceptance_criteria={"must": ["b1"]},
        created_at=t0,
        updated_at=t0,
    )
    s.add_all([epic_a, epic_b])
    s.flush()

    pr1 = PR(external_id="gh/foo#1", repo="foo", number=1, title="PR1", summary="impl A")
    pr2 = PR(external_id="gh/foo#2", repo="foo", number=2, title="PR2", summary="impl B")
    s.add_all([pr1, pr2])
    s.flush()

    m1 = Meeting(
        external_id="zoom/M-1",
        title="Sprint review A",
        started_at=t0,
        transcript_ref="s3://transcripts/m1",
    )
    s.add_all([m1])
    s.flush()

    t1 = Thread(
        external_id="slack/T-1",
        channel="#eng",
        summary="decision: ship A",
        last_message_at=t0,
    )
    s.add_all([t1])
    s.flush()

    edges = [
        Edge(
            src_kind="epic",
            src_id=epic_a.id,
            dst_kind="pr",
            dst_id=pr1.id,
            relation="implements",
        ),
        Edge(
            src_kind="epic",
            src_id=epic_b.id,
            dst_kind="pr",
            dst_id=pr2.id,
            relation="implements",
        ),
        Edge(
            src_kind="meeting",
            src_id=m1.id,
            dst_kind="epic",
            dst_id=epic_a.id,
            relation="discusses",
        ),
        Edge(
            src_kind="thread",
            src_id=t1.id,
            dst_kind="epic",
            dst_id=epic_a.id,
            relation="discusses",
        ),
    ]
    s.add_all(edges)
    s.flush()

    drift_a = DriftScore(
        epic_id=epic_a.id,
        layer="l2",
        score=0.42,
        ci_low=0.30,
        ci_high=0.55,
        computed_at=t0,
        judge_run_id="run-a-l2",
    )
    drift_b = DriftScore(
        epic_id=epic_b.id,
        layer="l0",
        score=0.10,
        ci_low=None,
        ci_high=None,
        computed_at=t0,
        judge_run_id=None,
    )
    s.add_all([drift_a, drift_b])
    s.commit()

    return {
        "epic_a": epic_a,
        "epic_b": epic_b,
        "pr1": pr1,
        "pr2": pr2,
        "m1": m1,
        "t1": t1,
        "drift_a": drift_a,
        "drift_b": drift_b,
    }


def test_markdown_byte_stable_across_runs(session: Session) -> None:
    _seed_exports(session)
    first = generate_markdown(session)
    second = generate_markdown(session)
    assert first == second
    assert isinstance(first, str)
    assert first.encode("utf-8") == second.encode("utf-8")


def test_markdown_includes_all_epic_external_ids(session: Session) -> None:
    _seed_exports(session)
    out = generate_markdown(session)
    # Both epic external IDs must appear in output.
    assert "LIN-A" in out
    assert "LIN-B" in out
    # Epic titles must appear.
    assert "Epic A title" in out
    assert "Epic B title" in out
    # PRs cited.
    assert "gh/foo#1" in out
    assert "gh/foo#2" in out
    # Meeting/thread for epic A cited.
    assert "zoom/M-1" in out
    assert "slack/T-1" in out


def test_csv_has_header_and_row_per_epic(session: Session) -> None:
    _seed_exports(session)
    out = generate_csv(session)
    reader = csv.reader(io.StringIO(out))
    rows = list(reader)
    # Header row + 1 row per epic = 3 rows.
    assert len(rows) == 3
    header = rows[0]
    assert "epic_external_id" in header
    # Stable across runs.
    out2 = generate_csv(session)
    assert out == out2
    # Rows ordered by epic external_id ASC.
    body = rows[1:]
    epic_ids = [r[header.index("epic_external_id")] for r in body]
    assert epic_ids == sorted(epic_ids)
    assert epic_ids == ["LIN-A", "LIN-B"]


def test_sarif_v2_1_0_schema_invariants(session: Session) -> None:
    _seed_exports(session)
    out = generate_sarif(session)
    # Byte-stable.
    assert out == generate_sarif(session)
    # Valid JSON.
    doc = json.loads(out)
    # Version.
    assert doc["version"] == "2.1.0"
    assert "$schema" in doc
    assert "runs" in doc
    assert isinstance(doc["runs"], list)
    assert len(doc["runs"]) == 1
    run = doc["runs"][0]
    # Driver metadata.
    assert run["tool"]["driver"]["name"] == "receipts"
    assert "version" in run["tool"]["driver"]
    # Results: one per drift_score row.
    results = run["results"]
    assert len(results) == 2
    for r in results:
        assert "ruleId" in r
        assert "level" in r
        assert "message" in r
        assert "locations" in r
        # Locations reference the epic external_id.
        assert any("LIN-" in json.dumps(loc) for loc in r["locations"])


def test_fhir_bundle_resourceType_and_type(session: Session) -> None:
    _seed_exports(session)
    out = generate_fhir_bundle(session)
    # Byte-stable.
    assert out == generate_fhir_bundle(session)
    doc = json.loads(out)
    assert doc["resourceType"] == "Bundle"
    assert doc["type"] == "collection"
    assert "entry" in doc
    # One Composition per epic.
    assert len(doc["entry"]) == 2
    for entry in doc["entry"]:
        comp = entry["resource"]
        assert comp["resourceType"] == "Composition"
        assert "subject" in comp
        assert "section" in comp
        assert isinstance(comp["section"], list)


def test_citations_present(session: Session) -> None:
    _seed_exports(session)

    md = generate_markdown(session)
    csv_out = generate_csv(session)
    sarif = generate_sarif(session)
    fhir = generate_fhir_bundle(session)

    # Every epic external_id must appear in every output.
    for output in (md, csv_out, sarif, fhir):
        assert "LIN-A" in output, "epic LIN-A missing from output"
        assert "LIN-B" in output, "epic LIN-B missing from output"

    # When PRs exist, at least one PR external_id must appear in every output.
    pr_ids = {"gh/foo#1", "gh/foo#2"}
    for output in (md, csv_out, sarif, fhir):
        assert any(pid in output for pid in pr_ids), (
            f"no PR external_id cited in output: {output[:200]}"
        )


def test_explicit_epic_external_ids_filter(session: Session) -> None:
    _seed_exports(session)
    # Subset selects only LIN-A.
    md = generate_markdown(session, epic_external_ids=["LIN-A"])
    assert "LIN-A" in md
    assert "LIN-B" not in md

    csv_out = generate_csv(session, epic_external_ids=["LIN-A"])
    reader = csv.reader(io.StringIO(csv_out))
    rows = list(reader)
    # Header + 1 row.
    assert len(rows) == 2
    header = rows[0]
    assert rows[1][header.index("epic_external_id")] == "LIN-A"
