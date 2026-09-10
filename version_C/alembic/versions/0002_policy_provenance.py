"""runs에 규정 코퍼스 출처 기록 추가

기재요령은 학년도마다 개정된다. 어느 규정 스냅샷(`policy_index_version`)의 어느
학년도(`policy_year`)로 검증했는지 남겨야, 코퍼스가 갱신된 뒤에도 "이 초안은 그때
그 규정으로는 통과였다"를 증명하고 불일치를 감지할 수 있다.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-10
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite는 ALTER를 거의 지원하지 않으므로 배치 모드로 테이블을 재작성한다.
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("policy_index_version", sa.String(64), nullable=True))
        batch.add_column(sa.Column("policy_year", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("policy_findings", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("policy_findings")
        batch.drop_column("policy_year")
        batch.drop_column("policy_index_version")
