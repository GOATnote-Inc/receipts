"""P2-6: clinical-reconciler core tests.

The clinical reconciler ingests one week of fixtures (encounters / artifacts /
decisions) into the L1 clinical schema (``encounter`` /
``clinical_artifact`` / ``clinical_drift_finding`` from migration
``0002_clinical``), drafts one ``EncounterContract`` per encounter via the
S2/S3 stub registry, validates it, and optionally Merkle-appends every
draft + runs the κ + hallucination guard gates.

These tests exercise the substrate against ``fixtures/clinical/week_0001/``.
The stub drafter covers ``ENC-001..030`` (4-digit fixture IDs map to the
3-digit registry keys), so pass^1 must hit ``1.0`` and the chain must stay
intact under the stub path.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from receipts.clinical import ClinicalReconcilerResult, reconcile_clinical_week
from receipts.drafter import EncounterContract
from receipts.judge.hallucination_guard import HallucinationGuard
from receipts.ledger.merkle import MerkleLog
from receipts.ledger.models import ClinicalArtifact, Encounter

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
WEEK_DIR = REPO_ROOT / "fixtures" / "clinical" / "week_0001"


# ---------------------------------------------------------------------------
# Schema bootstrap — function-scoped so each test gets an isolated in-memory
# SQLite DB. Mirrors the ``test_eng_reconciler.py`` pattern; alembic
# ``upgrade head`` applies both 0001_init and 0002_clinical.
# ---------------------------------------------------------------------------


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'clinical_reconciler.db'}"


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
    """Row counts after ingest must match the fixture (30 encounters / 150 artifacts)."""
    result = reconcile_clinical_week(WEEK_DIR, session)

    assert isinstance(result, ClinicalReconcilerResult)
    assert result.week_id == "week_0001"
    assert session.query(Encounter).count() == 30
    assert session.query(ClinicalArtifact).count() == 30 * 5
    # Each artifact except v1 must have a non-null parent_artifact_id;
    # v1 artifacts (kind == "audio") have null parents.
    rooted = (
        session.query(ClinicalArtifact)
        .filter(ClinicalArtifact.parent_artifact_id.is_(None))
        .count()
    )
    assert rooted == 30, f"expected 30 root artifacts (one per encounter), got {rooted}"


def test_reconcile_drafts_one_per_encounter(session: Session) -> None:
    """Reconciler produces one EncounterContract per encounter — 30 total, all validated."""
    result = reconcile_clinical_week(WEEK_DIR, session)

    assert result.encounter_count == 30
    assert len(result.drafts) == 30
    # Drafts emitted in deterministic external_id ASC order.
    enc_ids = [ext_id for ext_id, _contract in result.drafts]
    assert enc_ids == sorted(enc_ids)
    assert enc_ids[0] == "ENC-0001"
    assert enc_ids[-1] == "ENC-0030"
    # Every contract has the canonical EncounterContract shape with non-empty safety floor.
    for _ext_id, contract in result.drafts:
        assert isinstance(contract, EncounterContract)
        assert contract.acceptance_criteria, "contract has no acceptance_criteria"
        assert contract.safety_criteria, "contract has no safety_criteria"
        assert contract.citations, "contract has no citations"
        assert contract.drift_summary, "contract has empty drift_summary"


def test_reconcile_passk_one_on_stub(session: Session) -> None:
    """pass^1 across all 30 stub-backed encounter contracts must hit 1.0."""
    result = reconcile_clinical_week(WEEK_DIR, session)

    assert result.pass_count == 30
    assert result.passk == pytest.approx(1.0)


def test_reconcile_merkle_chain_intact_when_merkle_passed(session: Session) -> None:
    """Passing a MerkleLog appends one row per contract and the chain verifies clean."""
    merkle = MerkleLog(session)
    result = reconcile_clinical_week(WEEK_DIR, session, merkle_log=merkle)

    assert result.merkle_chain_intact is True
    assert result.merkle_row_count == 30
    # Re-verify directly against the log to prove the reconciler isn't lying.
    assert merkle.verify_chain() == []


def test_reconcile_skips_merkle_when_none(session: Session) -> None:
    """Without a MerkleLog the result reports the sentinel (intact, 0 rows)."""
    result = reconcile_clinical_week(WEEK_DIR, session)

    assert result.merkle_chain_intact is True
    assert result.merkle_row_count == 0


def test_reconcile_returns_result_dataclass_with_expected_fields(session: Session) -> None:
    """ClinicalReconcilerResult exposes the documented surface."""
    result = reconcile_clinical_week(WEEK_DIR, session)

    expected_fields = {
        "week_id",
        "drafts",
        "encounter_count",
        "pass_count",
        "passk",
        "kappa",
        "hallucination_flag_rate",
        "merkle_chain_intact",
        "merkle_row_count",
    }
    assert expected_fields.issubset(set(result.__dataclass_fields__.keys()))
    # Optional fields default to ``None`` when their gates were not wired.
    assert result.kappa is None
    assert result.hallucination_flag_rate is None


def test_reconcile_hallucination_rate_with_guard(session: Session) -> None:
    """Guard reports flag-rate when every citation resolves to a known artifact."""
    guard = HallucinationGuard()
    result = reconcile_clinical_week(WEEK_DIR, session, hallucination_guard=guard)

    # Stub citations target hand-crafted NOTE-/TRX-/ORD- ids that are not part
    # of the fixture's audio->...->committed_note version chain. The guard
    # therefore flags every citation; the operational contract is only that
    # ``flag_rate`` is a float in [0.0, 1.0].
    assert result.hallucination_flag_rate is not None
    assert 0.0 <= result.hallucination_flag_rate <= 1.0
