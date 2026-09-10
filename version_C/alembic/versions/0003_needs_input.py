"""runs.needs_input 추가

계약 복구가 1회 재시도 후에도 실패하거나 모델이 정보 부족을 선언하면, 지금까지는
`error`로 작업을 끝냈다. 그러면 교사는 무엇이 부족했는지 못 보고 작업만 사라진다.
`error`(코드가 처리할 수 없는 고장)와 구분되는 `needs_input`(사람이 채워야 하는 공백)을
따로 담아 검토 화면에 그대로 보여 준다.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-11
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("needs_input", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("needs_input")
