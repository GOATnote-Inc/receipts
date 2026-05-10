"""L1 init: temporal-graph schema (9 tables).

Revision ID: 0001_init
Revises:
Create Date: 2026-05-10

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_init"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "epic",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("acceptance_criteria", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_epic_external_id", "epic", ["external_id"], unique=True)

    op.create_table(
        "pr",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("repo", sa.String(length=255), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("merged_sha", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("merged_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_pr_external_id", "pr", ["external_id"], unique=True)

    op.create_table(
        "commit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sha", sa.String(length=64), nullable=False),
        sa.Column("repo", sa.String(length=255), nullable=False),
        sa.Column("author", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("committed_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_commit_sha", "commit", ["sha"], unique=True)

    op.create_table(
        "meeting",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("transcript_ref", sa.String(length=1024), nullable=False),
    )
    op.create_index("ix_meeting_external_id", "meeting", ["external_id"], unique=True)

    op.create_table(
        "thread",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("channel", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("last_message_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_thread_external_id", "thread", ["external_id"], unique=True)

    op.create_table(
        "edge",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("src_kind", sa.String(length=32), nullable=False),
        sa.Column("src_id", sa.Integer(), nullable=False),
        sa.Column("dst_kind", sa.String(length=32), nullable=False),
        sa.Column("dst_id", sa.Integer(), nullable=False),
        sa.Column("relation", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_edge_src_compound", "edge", ["src_kind", "src_id", "relation"])
    op.create_index("ix_edge_dst_compound", "edge", ["dst_kind", "dst_id", "relation"])

    op.create_table(
        "drift_score",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("epic_id", sa.Integer(), nullable=False),
        sa.Column("layer", sa.String(length=8), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("ci_low", sa.Float(), nullable=True),
        sa.Column("ci_high", sa.Float(), nullable=True),
        sa.Column("computed_at", sa.DateTime(), nullable=False),
        sa.Column("judge_run_id", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["epic_id"], ["epic.id"], ondelete="CASCADE", name="fk_drift_score_epic_id"
        ),
    )
    op.create_index("ix_drift_score_epic_id", "drift_score", ["epic_id"])
    op.create_index("ix_drift_score_judge_run_id", "drift_score", ["judge_run_id"])

    op.create_table(
        "judge_rationale",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("judge_run_id", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_sha", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_text", sa.Text(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_judge_rationale_judge_run_id", "judge_rationale", ["judge_run_id"], unique=True
    )

    op.create_table(
        "attestation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("target_kind", sa.String(length=32), nullable=False),
        sa.Column("hash", sa.String(length=128), nullable=False),
        sa.Column("prev_hash", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_attestation_hash", "attestation", ["hash"])


def downgrade() -> None:
    op.drop_index("ix_attestation_hash", table_name="attestation")
    op.drop_table("attestation")

    op.drop_index("ix_judge_rationale_judge_run_id", table_name="judge_rationale")
    op.drop_table("judge_rationale")

    op.drop_index("ix_drift_score_judge_run_id", table_name="drift_score")
    op.drop_index("ix_drift_score_epic_id", table_name="drift_score")
    op.drop_table("drift_score")

    op.drop_index("ix_edge_dst_compound", table_name="edge")
    op.drop_index("ix_edge_src_compound", table_name="edge")
    op.drop_table("edge")

    op.drop_index("ix_thread_external_id", table_name="thread")
    op.drop_table("thread")

    op.drop_index("ix_meeting_external_id", table_name="meeting")
    op.drop_table("meeting")

    op.drop_index("ix_commit_sha", table_name="commit")
    op.drop_table("commit")

    op.drop_index("ix_pr_external_id", table_name="pr")
    op.drop_table("pr")

    op.drop_index("ix_epic_external_id", table_name="epic")
    op.drop_table("epic")
