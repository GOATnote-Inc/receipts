"""L1 clinical-schema tests: alembic 0002 adds three clinical tables on top of 0001.

All tests run against an isolated SQLite database per tmp_path. Alembic
``upgrade head`` must apply 0001 then 0002 in order, preserving the eng tables
and adding ``encounter``, ``clinical_artifact``, ``clinical_drift_finding``.

The clinical schema deliberately does NOT store plaintext patient identifiers:
- ``encounter.patient_id_hash`` is a string (hashed external ID)
- audio / note bodies live on L5 ObjectLockStore; only ``content_ref`` (path)
  and ``content_hash`` (digest) are in the DB.
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

CLINICAL_TABLES = {"encounter", "clinical_artifact", "clinical_drift_finding"}
ENG_TABLES = {
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


def test_alembic_upgrade_creates_clinical_tables(upgraded_engine) -> None:
    """0001 + 0002 applied in order; eng tables preserved, clinical tables present."""
    inspector = inspect(upgraded_engine)
    tables = set(inspector.get_table_names())

    missing_clin = CLINICAL_TABLES - tables
    assert not missing_clin, f"clinical tables missing after upgrade head: {missing_clin}"

    missing_eng = ENG_TABLES - tables
    assert not missing_eng, (
        f"eng tables disappeared after 0002_clinical; 0001 likely not preserved: {missing_eng}"
    )

    enc_cols = {c["name"] for c in inspector.get_columns("encounter")}
    assert {
        "id",
        "external_id",
        "patient_id_hash",
        "started_at",
        "chief_complaint",
        "status",
        "created_at",
    } <= enc_cols, f"encounter missing columns; have {enc_cols}"
    assert "patient_id" not in enc_cols, (
        "encounter MUST NOT carry plaintext patient_id; use patient_id_hash only"
    )

    art_cols = {c["name"] for c in inspector.get_columns("clinical_artifact")}
    assert {
        "id",
        "encounter_id",
        "kind",
        "content_ref",
        "content_hash",
        "version",
        "parent_artifact_id",
        "created_at",
    } <= art_cols, f"clinical_artifact missing columns; have {art_cols}"

    drift_cols = {c["name"] for c in inspector.get_columns("clinical_drift_finding")}
    assert {
        "id",
        "encounter_id",
        "artifact_id",
        "layer",
        "rule_name",
        "severity",
        "message",
        "score",
        "ci_low",
        "ci_high",
        "created_at",
    } <= drift_cols, f"clinical_drift_finding missing columns; have {drift_cols}"

    enc_indexed: set[str] = set()
    for ix in inspector.get_indexes("encounter"):
        if len(ix["column_names"]) == 1:
            enc_indexed.add(ix["column_names"][0])
    for uc in inspector.get_unique_constraints("encounter"):
        if len(uc["column_names"]) == 1:
            enc_indexed.add(uc["column_names"][0])
    assert "external_id" in enc_indexed, "encounter.external_id must be indexed"
    assert "patient_id_hash" in enc_indexed, "encounter.patient_id_hash must be indexed"

    art_indexed_single: set[str] = set()
    for ix in inspector.get_indexes("clinical_artifact"):
        if len(ix["column_names"]) == 1:
            art_indexed_single.add(ix["column_names"][0])
    assert "content_hash" in art_indexed_single, "clinical_artifact.content_hash must be indexed"


def test_encounter_external_id_unique_constraint(upgraded_engine) -> None:
    from receipts.ledger.models import Encounter

    Session = sessionmaker(bind=upgraded_engine)
    with Session() as s:
        s.add(
            Encounter(
                external_id="ENC-001",
                patient_id_hash="sha256:abc",
                started_at=__import__("datetime").datetime(2026, 5, 10, 9, 0),
                chief_complaint="chest pain",
            )
        )
        s.commit()

    with Session() as s:
        s.add(
            Encounter(
                external_id="ENC-001",
                patient_id_hash="sha256:def",
                started_at=__import__("datetime").datetime(2026, 5, 10, 10, 0),
                chief_complaint="duplicate id",
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()


def test_clinical_artifact_fk_cascade_on_encounter_delete(upgraded_engine) -> None:
    from receipts.ledger.models import (
        ClinicalArtifact,
        ClinicalDriftFinding,
        Encounter,
    )

    Session = sessionmaker(bind=upgraded_engine)
    import datetime as _dt

    with Session() as s:
        s.execute(text("PRAGMA foreign_keys=ON"))
        enc = Encounter(
            external_id="ENC-cascade",
            patient_id_hash="sha256:cascade",
            started_at=_dt.datetime(2026, 5, 10, 9, 0),
            chief_complaint="cascade test",
        )
        s.add(enc)
        s.flush()
        enc_id = enc.id
        art = ClinicalArtifact(
            encounter_id=enc_id,
            kind="audio",
            content_ref="s3://bucket/enc-cascade/audio-0.wav",
            content_hash="sha256:audio0",
            version=1,
        )
        s.add(art)
        s.flush()
        s.add(
            ClinicalDriftFinding(
                encounter_id=enc_id,
                artifact_id=art.id,
                layer="l1",
                rule_name="missing-hpi",
                severity="warn",
                message="HPI absent",
                score=0.4,
            )
        )
        s.commit()

    with Session() as s:
        s.execute(text("PRAGMA foreign_keys=ON"))
        assert s.query(ClinicalArtifact).count() == 1
        assert s.query(ClinicalDriftFinding).count() == 1
        enc = s.get(Encounter, enc_id)
        s.delete(enc)
        s.commit()
        assert s.query(ClinicalArtifact).count() == 0, (
            "clinical_artifact should cascade-delete with encounter"
        )
        assert s.query(ClinicalDriftFinding).count() == 0, (
            "clinical_drift_finding should cascade-delete with encounter"
        )


def test_clinical_artifact_compound_index_encounter_version(upgraded_engine) -> None:
    inspector = inspect(upgraded_engine)
    indexes = inspector.get_indexes("clinical_artifact")
    cols = [tuple(ix["column_names"]) for ix in indexes]
    assert ("encounter_id", "version") in cols, (
        f"clinical_artifact compound index (encounter_id, version) missing; have: {cols}"
    )


def test_clinical_drift_finding_artifact_fk_nullable(upgraded_engine) -> None:
    """artifact_id is nullable so encounter-level findings can be recorded."""
    inspector = inspect(upgraded_engine)
    cols = {c["name"]: c for c in inspector.get_columns("clinical_drift_finding")}
    assert "artifact_id" in cols, "clinical_drift_finding.artifact_id missing"
    assert cols["artifact_id"]["nullable"] is True, (
        "clinical_drift_finding.artifact_id must be nullable"
    )

    from receipts.ledger.models import ClinicalDriftFinding, Encounter

    Session = sessionmaker(bind=upgraded_engine)
    import datetime as _dt

    with Session() as s:
        s.execute(text("PRAGMA foreign_keys=ON"))
        enc = Encounter(
            external_id="ENC-nullable",
            patient_id_hash="sha256:nullable",
            started_at=_dt.datetime(2026, 5, 10, 9, 0),
            chief_complaint="nullable test",
        )
        s.add(enc)
        s.flush()
        s.add(
            ClinicalDriftFinding(
                encounter_id=enc.id,
                artifact_id=None,
                layer="l0",
                rule_name="encounter-level",
                severity="info",
                message="ok",
                score=0.9,
            )
        )
        s.commit()
        assert s.query(ClinicalDriftFinding).count() == 1
