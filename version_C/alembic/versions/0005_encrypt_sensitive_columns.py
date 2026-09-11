"""runs.student_key, pseudonym_maps.mapping를 Text로 확장 (컬럼 암호화 대비)

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-11
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.alter_column("student_key", type_=sa.Text(), existing_type=sa.String(128))
    with op.batch_alter_table("pseudonym_maps") as batch:
        batch.alter_column("mapping", type_=sa.Text(), existing_type=sa.JSON())


def downgrade() -> None:
    with op.batch_alter_table("pseudonym_maps") as batch:
        batch.alter_column("mapping", type_=sa.JSON(), existing_type=sa.Text())
    with op.batch_alter_table("runs") as batch:
        batch.alter_column("student_key", type_=sa.String(128), existing_type=sa.Text())
