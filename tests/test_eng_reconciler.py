"""P1-6: reconciler-core tests.

The reconciler ingests a weekly fixture (epics / prs / commits / meetings /
threads) into the L1 temporal graph, traverses each epic's lineage via
``LineageQuery``, drafts a ``RevisedSpec`` per epic, validates it, and
optionally Merkle-appends every draft + computes κ + hallucination flag-rate.

These tests exercise the substrate against ``fixtures/eng/week_0001/``. The
stub drafter (S1/S3 registry, ``EPIC-001..030``) passes validation on every
epic in that corpus, so pass^1 must hit ``1.0`` and the chain must stay intact.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from receipts.eng import ReconcilerResult, reconcile_week
from receipts.judge.hallucination_guard import HallucinationGuard
from receipts.ledger.merkle import MerkleLog
from receipts.ledger.models import (
    PR,
    Commit,
    Edge,
    Epic,
    Meeting,
    Thread,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
WEEK_DIR = REPO_ROOT / "fixtures" / "eng" / "week_0001"


# ---------------------------------------------------------------------------
# Schema bootstrap — function-scoped so each test gets an isolated in-memory
# SQLite DB. We follow the same pattern as ``tests/test_ledger_schema.py`` but
# point at a tmpdir file URL because alembic needs a path it can resolve.
# ---------------------------------------------------------------------------


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'reconciler.db'}"


@pytest.fixture
def session(db_url: str) -> Iterator[Session]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(cfg, "head")
    engine = create_engine(db_url)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_reconcile_ingests_all_tables(session: Session) -> None:
    """Row counts after ingest must match the fixture (30/200/497/30/500)."""
    result = reconcile_week(WEEK_DIR, session)

    assert isinstance(result, ReconcilerResult)
    assert result.week_id == "week_0001"
    assert session.query(Epic).count() == 30
    assert session.query(PR).count() == 200
    assert session.query(Commit).count() == 497
    assert session.query(Meeting).count() == 30
    assert session.query(Thread).count() == 500
    # Edges materialised from inline relationships: 200 epic->pr + N
    # meeting->epic + ~340 thread->epic + 497 pr->commit. Lower-bound check
    # mirrors the substrate E2E assertion.
    assert session.query(Edge).count() > 200 + 340 + 497


def test_reconcile_drafts_one_per_epic(session: Session) -> None:
    """Reconciler produces one RevisedSpec per epic — 30 total, all validated."""
    result = reconcile_week(WEEK_DIR, session)

    assert result.epic_count == 30
    assert len(result.drafts) == 30
    # Drafts are emitted in deterministic external-id ASC order.
    epic_ids = [ext_id for ext_id, _spec in result.drafts]
    assert epic_ids == sorted(epic_ids)
    assert epic_ids[0] == "EPIC-0001"
    assert epic_ids[-1] == "EPIC-0030"
    # Every spec carries the canonical RevisedSpec shape.
    for _ext_id, spec in result.drafts:
        assert spec.acceptance_criteria, "spec has no acceptance_criteria"
        assert spec.citations, "spec has no citations"
        assert spec.drift_summary, "spec has empty drift_summary"


def test_reconcile_passk_one_on_stub(session: Session) -> None:
    """pass^1 across all 30 stub-backed drafts must hit 1.0."""
    result = reconcile_week(WEEK_DIR, session)

    assert result.pass_count == 30
    assert result.passk == pytest.approx(1.0)


def test_reconcile_merkle_chain_intact_when_merkle_passed(session: Session) -> None:
    """Passing a MerkleLog appends one row per draft and the chain verifies clean."""
    merkle = MerkleLog(session)
    result = reconcile_week(WEEK_DIR, session, merkle_log=merkle)

    assert result.merkle_chain_intact is True
    assert result.merkle_row_count == 30
    # Re-verify directly against the log to prove the reconciler isn't lying.
    assert merkle.verify_chain() == []


def test_reconcile_skips_merkle_when_none(session: Session) -> None:
    """Without a MerkleLog the result reports the sentinel (intact, 0 rows)."""
    result = reconcile_week(WEEK_DIR, session)

    assert result.merkle_chain_intact is True
    assert result.merkle_row_count == 0


def test_reconcile_hallucination_rate_with_guard(session: Session) -> None:
    """Guard sees every citation resolve to a real artifact ⇒ flag-rate is 0."""
    guard = HallucinationGuard()
    result = reconcile_week(WEEK_DIR, session, hallucination_guard=guard)

    assert result.hallucination_flag_rate == pytest.approx(0.0)
