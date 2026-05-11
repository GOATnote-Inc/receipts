"""SQLAlchemy 2.0 declarative models for the temporal-graph ledger.

Nine tables back the append-only intent-vs-execution substrate:

- epic / pr / commit / meeting / thread — first-class artifact nodes
- edge — typed relations between any pair of nodes (polymorphic via kind+id)
- drift_score — CEIS L0/L1/L2 outputs against an epic
- judge_rationale — full LLM judge invocation record
- attestation — backing store for Merkle log (chain logic lives in L2)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from receipts.ledger.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Epic(Base):
    __tablename__ = "epic"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    acceptance_criteria: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )


class PR(Base):
    __tablename__ = "pr"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    merged_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    merged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class Commit(Base):
    __tablename__ = "commit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sha: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    repo: Mapped[str] = mapped_column(String(255), nullable=False)
    author: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    committed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class Meeting(Base):
    __tablename__ = "meeting"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    transcript_ref: Mapped[str] = mapped_column(String(1024), nullable=False, default="")


class Thread(Base):
    __tablename__ = "thread"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    channel: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_message_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class Edge(Base):
    """Polymorphic edge: any kind+id node pair, plus a relation string."""

    __tablename__ = "edge"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    src_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    src_id: Mapped[int] = mapped_column(Integer, nullable=False)
    dst_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    dst_id: Mapped[int] = mapped_column(Integer, nullable=False)
    relation: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    __table_args__ = (
        Index("ix_edge_src_compound", "src_kind", "src_id", "relation"),
        Index("ix_edge_dst_compound", "dst_kind", "dst_id", "relation"),
    )


class DriftScore(Base):
    __tablename__ = "drift_score"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    epic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("epic.id", ondelete="CASCADE"), nullable=False, index=True
    )
    layer: Mapped[str] = mapped_column(String(8), nullable=False)  # "l0" | "l1" | "l2"
    score: Mapped[float] = mapped_column(Float, nullable=False)
    ci_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    ci_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    judge_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


class JudgeRationale(Base):
    __tablename__ = "judge_rationale"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    judge_run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_text: Mapped[str] = mapped_column(Text, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class Attestation(Base):
    """Backing row for the Merkle log; chain logic lives in L2 (ledger/merkle.py)."""

    __tablename__ = "attestation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False)
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    prev_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[Any] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


# --------------------------------------------------------------------------- #
# Clinical Audit Ledger (Phase 2)                                              #
#                                                                              #
# These three tables back the CMIO-facing clinical-workflow attestation. They  #
# share the same Base / engine as the eng tables but live conceptually in a    #
# separate vertical: scribe audio -> AI note -> edited/committed note +        #
# associated EHR orders.                                                       #
#                                                                              #
# PHI discipline (enforced at the schema layer, not just docs):                #
#   - encounter.patient_id_hash is a string only; the plaintext patient ID     #
#     never enters this DB. Re-identification is out-of-scope mapping store.   #
#   - clinical_artifact carries content_ref (path on L5 ObjectLockStore) and   #
#     content_hash; audio + note bodies are NEVER stored in-band here.         #
# --------------------------------------------------------------------------- #


class Encounter(Base):
    """A clinical encounter: one chart visit, possibly many artifacts."""

    __tablename__ = "encounter"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    patient_id_hash: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    chief_complaint: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


class ClinicalArtifact(Base):
    """Versioned artifact attached to an encounter.

    ``kind`` is a free-string with a documented enum-like set of values:
    ``audio``, ``transcript``, ``ai_note``, ``edited_note``, ``committed_note``,
    ``order``. Body content lives on L5 ObjectLockStore; this row only carries
    ``content_ref`` (path) and ``content_hash`` (digest).

    ``parent_artifact_id`` lets us chain ``audio -> transcript -> ai_note ->
    edited_note -> committed_note`` and inspect provenance.
    """

    __tablename__ = "clinical_artifact"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    encounter_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("encounter.id", ondelete="CASCADE", name="fk_clinical_artifact_encounter_id"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    content_ref: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parent_artifact_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "clinical_artifact.id",
            ondelete="SET NULL",
            name="fk_clinical_artifact_parent_id",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    __table_args__ = (Index("ix_clinical_artifact_encounter_version", "encounter_id", "version"),)


class ClinicalDriftFinding(Base):
    """L0/L1/L2 finding attached to an encounter (and optionally a specific artifact).

    ``artifact_id`` is nullable so encounter-level findings (e.g. "no AI note
    produced") can be recorded without forcing a synthetic artifact row.
    """

    __tablename__ = "clinical_drift_finding"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    encounter_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "encounter.id", ondelete="CASCADE", name="fk_clinical_drift_finding_encounter_id"
        ),
        nullable=False,
        index=True,
    )
    artifact_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "clinical_artifact.id",
            ondelete="SET NULL",
            name="fk_clinical_drift_finding_artifact_id",
        ),
        nullable=True,
    )
    layer: Mapped[str] = mapped_column(String(8), nullable=False)  # "l0" | "l1" | "l2"
    rule_name: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)  # info | warn | critical
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    score: Mapped[float] = mapped_column(Float, nullable=False)
    ci_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    ci_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)


__all__ = [
    "Attestation",
    "ClinicalArtifact",
    "ClinicalDriftFinding",
    "Commit",
    "DriftScore",
    "Edge",
    "Encounter",
    "Epic",
    "JudgeRationale",
    "Meeting",
    "PR",
    "Thread",
]
