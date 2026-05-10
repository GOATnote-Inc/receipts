"""L2 Merkle chain tests.

The chain is a SHA-256 linkage over canonical-JSON payloads. Each
attestation row stores its own hash and the hash of the immediately
preceding row (by id ASC). Genesis row stores prev_hash = "".

Tests:
- genesis append: prev_hash = ""
- multi-row append: each row's prev_hash equals the prior row's hash
- hash determinism: same (payload, prev_hash) -> same hash
- verify_chain intact: returns []
- verify_chain after manual payload mutation: returns the bad row's id
- tamper_detect: identifies the mutated row
- perf: verify_chain over 1000 rows completes in < 1s
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from receipts.ledger.merkle import MerkleLog, compute_hash
from receipts.ledger.models import Attestation

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


def _expected_hash(payload: dict, prev_hash: str) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((canonical + prev_hash).encode("utf-8")).hexdigest()


def test_genesis_append_has_empty_prev_hash(session: Session) -> None:
    log = MerkleLog(session)
    payload = {"event": "linear.epic.created", "external_id": "LIN-1"}
    h = log.append(payload, kind="linear.epic.created", target_id=1, target_kind="epic")

    row = session.query(Attestation).order_by(Attestation.id.asc()).first()
    assert row is not None
    assert row.prev_hash == ""
    assert row.hash == h
    assert h == _expected_hash(payload, "")


def test_multi_row_append_links_prev_hash(session: Session) -> None:
    log = MerkleLog(session)
    h1 = log.append({"n": 1}, kind="t.a", target_id=1, target_kind="epic")
    h2 = log.append({"n": 2}, kind="t.a", target_id=2, target_kind="epic")
    h3 = log.append({"n": 3}, kind="t.a", target_id=3, target_kind="epic")

    rows = session.query(Attestation).order_by(Attestation.id.asc()).all()
    assert len(rows) == 3
    assert rows[0].prev_hash == ""
    assert rows[1].prev_hash == h1
    assert rows[2].prev_hash == h2
    assert rows[0].hash == h1
    assert rows[1].hash == h2
    assert rows[2].hash == h3


def test_compute_hash_is_deterministic() -> None:
    payload_a = {"b": 2, "a": 1}
    payload_b = {"a": 1, "b": 2}  # different key order, same content
    prev = "abc123"
    h1 = compute_hash(payload_a, prev)
    h2 = compute_hash(payload_b, prev)
    assert h1 == h2 == _expected_hash(payload_a, prev)


def test_verify_chain_intact_returns_empty_list(session: Session) -> None:
    log = MerkleLog(session)
    for i in range(5):
        log.append({"i": i}, kind="t.a", target_id=i, target_kind="epic")
    assert log.verify_chain() == []


def test_verify_chain_after_payload_mutation_flags_row(session: Session) -> None:
    log = MerkleLog(session)
    for i in range(5):
        log.append({"i": i}, kind="t.a", target_id=i, target_kind="epic")

    # Mutate row 3's payload directly in DB (simulating tamper).
    rows = session.query(Attestation).order_by(Attestation.id.asc()).all()
    bad_id = rows[2].id
    rows[2].payload = {"i": "TAMPERED"}
    session.commit()

    bad = log.verify_chain()
    assert bad_id in bad
    assert bad != []


def test_tamper_detect_identifies_mutated_row(session: Session) -> None:
    log = MerkleLog(session)
    h1 = log.append({"a": 1}, kind="t.a", target_id=1, target_kind="epic")
    h2 = log.append({"b": 2}, kind="t.a", target_id=2, target_kind="epic")
    h3 = log.append({"c": 3}, kind="t.a", target_id=3, target_kind="epic")
    assert h1 and h2 and h3

    rows = session.query(Attestation).order_by(Attestation.id.asc()).all()
    bad_id = rows[1].id
    rows[1].payload = {"b": 999}
    session.commit()

    tampered = log.tamper_detect()
    assert bad_id in tampered


def test_verify_chain_1000_rows_under_one_second(session: Session) -> None:
    log = MerkleLog(session)
    for i in range(1000):
        log.append({"i": i}, kind="t.a", target_id=i, target_kind="epic")

    t0 = time.perf_counter()
    bad = log.verify_chain()
    elapsed = time.perf_counter() - t0
    assert bad == []
    assert elapsed < 1.0, f"verify_chain too slow: {elapsed:.3f}s"
