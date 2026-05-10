"""L1 schema tests: alembic migration creates the 9-table temporal graph.

All tests run against an isolated SQLite database (file-based since alembic
config expects a URL, but we point it at a tmpdir file per test).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from alembic import command

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

EXPECTED_TABLES = {
    "epic",
    "pr",
    "commit",
    "meeting",
    "thread",
    "edge",
    "drift_score",
    "judge_rationale",
    "attestation",
}


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


def test_alembic_upgrade_head_creates_all_tables(upgraded_engine) -> None:
    inspector = inspect(upgraded_engine)
    tables = set(inspector.get_table_names())
    missing = EXPECTED_TABLES - tables
    assert not missing, f"missing tables after upgrade head: {missing}"

    # Spot-check key columns on each business table.
    epic_cols = {c["name"] for c in inspector.get_columns("epic")}
    assert {
        "id",
        "external_id",
        "title",
        "acceptance_criteria",
        "created_at",
        "updated_at",
    } <= epic_cols

    pr_cols = {c["name"] for c in inspector.get_columns("pr")}
    assert {"id", "external_id", "repo", "number", "merged_sha", "merged_at"} <= pr_cols

    commit_cols = {c["name"] for c in inspector.get_columns("commit")}
    assert {"id", "sha", "repo", "author", "message", "committed_at"} <= commit_cols

    drift_cols = {c["name"] for c in inspector.get_columns("drift_score")}
    assert {"id", "epic_id", "layer", "score", "ci_low", "ci_high", "judge_run_id"} <= drift_cols

    judge_cols = {c["name"] for c in inspector.get_columns("judge_rationale")}
    assert {
        "id",
        "judge_run_id",
        "model",
        "prompt_sha",
        "request_hash",
        "response_text",
    } <= judge_cols

    att_cols = {c["name"] for c in inspector.get_columns("attestation")}
    assert {"id", "kind", "target_id", "target_kind", "hash", "prev_hash", "payload"} <= att_cols


def test_epic_external_id_unique_constraint(upgraded_engine) -> None:
    from receipts.ledger.models import Epic

    Session = sessionmaker(bind=upgraded_engine)
    with Session() as s:
        s.add(Epic(external_id="LIN-1", title="First", acceptance_criteria={"must": []}))
        s.commit()

    with Session() as s:
        s.add(Epic(external_id="LIN-1", title="Duplicate", acceptance_criteria={}))
        with pytest.raises(IntegrityError):
            s.commit()


def test_edge_compound_indexes_exist(upgraded_engine) -> None:
    inspector = inspect(upgraded_engine)
    edge_indexes = inspector.get_indexes("edge")
    index_columns = [tuple(ix["column_names"]) for ix in edge_indexes]

    assert ("src_kind", "src_id", "relation") in index_columns, (
        f"src compound index missing; have: {index_columns}"
    )
    assert ("dst_kind", "dst_id", "relation") in index_columns, (
        f"dst compound index missing; have: {index_columns}"
    )


def test_drift_score_fk_cascade(upgraded_engine) -> None:
    from receipts.ledger.models import DriftScore, Epic

    Session = sessionmaker(bind=upgraded_engine)
    with Session() as s:
        # SQLite needs explicit FK pragma per connection.
        s.execute(text("PRAGMA foreign_keys=ON"))
        epic = Epic(external_id="LIN-cascade", title="Will be deleted", acceptance_criteria=[])
        s.add(epic)
        s.flush()
        epic_id = epic.id
        s.add(DriftScore(epic_id=epic_id, layer="l1", score=0.42))
        s.commit()

    with Session() as s:
        s.execute(text("PRAGMA foreign_keys=ON"))
        assert s.query(DriftScore).count() == 1
        epic = s.get(Epic, epic_id)
        s.delete(epic)
        s.commit()
        assert s.query(DriftScore).count() == 0, "drift_score should cascade-delete with epic"


def test_attestation_hash_indexed(upgraded_engine) -> None:
    inspector = inspect(upgraded_engine)
    att_indexes = inspector.get_indexes("attestation")
    indexed_cols: set[str] = set()
    for ix in att_indexes:
        if len(ix["column_names"]) == 1:
            indexed_cols.add(ix["column_names"][0])

    # Hash column should appear in at least one single-column index.
    assert "hash" in indexed_cols, f"attestation.hash not indexed; indexes seen: {att_indexes}"


def test_external_id_unique_indices_across_business_tables(upgraded_engine) -> None:
    inspector = inspect(upgraded_engine)
    for table in ("epic", "pr", "meeting", "thread"):
        unique_cols: set[str] = set()
        for ix in inspector.get_indexes(table):
            if ix.get("unique") and len(ix["column_names"]) == 1:
                unique_cols.add(ix["column_names"][0])
        # SQLAlchemy may model unique=True as a unique constraint rather than a unique index.
        for uc in inspector.get_unique_constraints(table):
            if len(uc["column_names"]) == 1:
                unique_cols.add(uc["column_names"][0])
        assert "external_id" in unique_cols, f"{table}.external_id must be unique"
