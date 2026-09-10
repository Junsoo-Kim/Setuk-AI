"""runs / reviews / artifacts / audit_logs 최초 스키마

메모리 `_RUNS`를 대체하는 영속 작업 목록. run.id는 LangGraph thread_id와 같은 값이라
체크포인트와 메타데이터가 항상 같은 작업을 가리킨다.

Revision ID: 0001
Revises:
Create Date: 2026-09-10
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("student_key", sa.String(128), nullable=False),
        sa.Column("pseudonym", sa.String(32), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("report_path", sa.String(512)),
        sa.Column("yaml_path", sa.String(512)),
        sa.Column("output_path", sa.String(512)),
        sa.Column("draft_text", sa.Text()),
        sa.Column("draft_hash", sa.String(64)),
        sa.Column("lint_summary", sa.Text()),
        sa.Column("lint_retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lint_passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error", sa.Text()),
        sa.Column("warnings", sa.JSON()),
        sa.Column("prompt_version", sa.String(64)),
        sa.Column("model_name", sa.String(64)),
        sa.Column("lease_owner", sa.String(64)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_runs_status", "runs", ["status"])
    op.create_index("ix_runs_pseudonym", "runs", ["pseudonym"])

    op.create_table(
        "reviews",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(64),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reviewer", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("draft_hash", sa.String(64)),
        sa.Column("approved_hash", sa.String(64)),
        sa.Column("edited", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("prompt_version", sa.String(64)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # 같은 승인 버튼을 두 번 눌러도 resume이 두 번 걸리지 않게 하는 핵심 제약.
        sa.UniqueConstraint("run_id", "idempotency_key", name="uq_review_idempotency"),
    )
    op.create_index("ix_reviews_run_id", "reviews", ["run_id"])

    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(64),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_artifacts_run_id", "artifacts", ["run_id"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(64)),
        sa.Column("pseudonym", sa.String(32)),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("detail", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_logs_run_id", "audit_logs", ["run_id"])
    op.create_index("ix_audit_logs_pseudonym", "audit_logs", ["pseudonym"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("artifacts")
    op.drop_table("reviews")
    op.drop_index("ix_runs_pseudonym", table_name="runs")
    op.drop_index("ix_runs_status", table_name="runs")
    op.drop_table("runs")
