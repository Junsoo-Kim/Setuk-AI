"""pseudonym_maps 테이블 추가

실명 ↔ 별칭 치환표를 LangGraph 체크포인트(state)가 아니라 이 앱 DB의 별도 테이블로
분리한다. 계획서 P4의 "원본 데이터와 가명 매핑 분리" 요건을 지키기 위함이다.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-11
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pseudonym_maps",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("mapping", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("pseudonym_maps")
