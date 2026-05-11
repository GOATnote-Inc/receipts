"""L1 clinical: add encounter / clinical_artifact / clinical_drift_finding.

Revision ID: 0002_clinical
Revises: 0001_init
Create Date: 2026-05-10

Adds three tables backing the Clinical Audit Ledger vertical on top of the
9-table eng substrate from 0001. Eng tables are NOT touched.

PHI discipline at the schema layer:
- ``encounter.patient_id_hash`` stores a hashed identifier only; the plaintext
  patient ID never enters this DB.
- ``clinical_artifact.content_ref`` + ``content_hash`` reference content
  stored externally on L5 ObjectLockStore (HIPAA-compliant bucket).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_clinical"
down_revision: str | None = "0001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "encounter",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("patient_id_hash", sa.String(length=128), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("chief_complaint", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_encounter_external_id", "encounter", ["external_id"], unique=True)
    op.create_index("ix_encounter_patient_id_hash", "encounter", ["patient_id_hash"])

    op.create_table(
        "clinical_artifact",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("encounter_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("content_ref", sa.String(length=1024), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("parent_artifact_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["encounter_id"],
            ["encounter.id"],
            ondelete="CASCADE",
            name="fk_clinical_artifact_encounter_id",
        ),
        sa.ForeignKeyConstraint(
            ["parent_artifact_id"],
            ["clinical_artifact.id"],
            ondelete="SET NULL",
            name="fk_clinical_artifact_parent_id",
        ),
    )
    op.create_index(
        "ix_clinical_artifact_content_hash", "clinical_artifact", ["content_hash"]
    )
    op.create_index(
        "ix_clinical_artifact_encounter_version",
        "clinical_artifact",
        ["encounter_id", "version"],
    )

    op.create_table(
        "clinical_drift_finding",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("encounter_id", sa.Integer(), nullable=False),
        sa.Column("artifact_id", sa.Integer(), nullable=True),
        sa.Column("layer", sa.String(length=8), nullable=False),
        sa.Column("rule_name", sa.String(length=128), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("ci_low", sa.Float(), nullable=True),
        sa.Column("ci_high", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["encounter_id"],
            ["encounter.id"],
            ondelete="CASCADE",
            name="fk_clinical_drift_finding_encounter_id",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["clinical_artifact.id"],
            ondelete="SET NULL",
            name="fk_clinical_drift_finding_artifact_id",
        ),
    )
    op.create_index(
        "ix_clinical_drift_finding_encounter_id",
        "clinical_drift_finding",
        ["encounter_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_clinical_drift_finding_encounter_id", table_name="clinical_drift_finding"
    )
    op.drop_table("clinical_drift_finding")

    op.drop_index(
        "ix_clinical_artifact_encounter_version", table_name="clinical_artifact"
    )
    op.drop_index("ix_clinical_artifact_content_hash", table_name="clinical_artifact")
    op.drop_table("clinical_artifact")

    op.drop_index("ix_encounter_patient_id_hash", table_name="encounter")
    op.drop_index("ix_encounter_external_id", table_name="encounter")
    op.drop_table("encounter")
