"""teachers 테이블과 runs.owner 추가 (RBAC)

계획서 P4의 "사용자·검토자·관리자 RBAC", 완료조건 "권한 없는 사용자가 다른 학생의
실행 결과에 접근하지 못함"을 지키기 위한 최소 인증·권한 기반이다. teacher 역할은
자신이 만든 run만, admin 역할은 전체를 볼 수 있다.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-11
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "teachers",
        sa.Column("username", sa.String(64), primary_key=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="teacher"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("owner", sa.String(64), nullable=True))
    op.create_index("ix_runs_owner", "runs", ["owner"])


def downgrade() -> None:
    op.drop_index("ix_runs_owner", table_name="runs")
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("owner")
    op.drop_table("teachers")
