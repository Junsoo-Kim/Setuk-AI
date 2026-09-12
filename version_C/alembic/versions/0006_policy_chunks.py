"""policy_chunks 테이블 추가 (PostgreSQL FTS + pgvector)

규정 검색을 파이썬 인메모리 BM25에서 PostgreSQL로 옮긴다. `search_tokens`는
`policy.retrieval.tokenize`와 같은 한국어 접미사 제거·바이그램 전처리를 거친
문자열이며, `to_tsvector('simple', ...)`로 만든 생성 컬럼을 GIN 인덱스로 검색한다.
`embedding`은 pgvector 컬럼으로, 임베딩 제공자가 설정된 경우에만 채워진다.

SQLite 배포(기본값)에는 적용하지 않는다 — `DATABASE_URL`이 PostgreSQL일 때만
`alembic upgrade head`로 이 테이블이 생긴다.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-11
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import TSVECTOR

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBEDDING_DIM = 1024


def upgrade() -> None:
    # 이 파일 docstring의 약속("SQLite 배포에는 적용하지 않는다")을 실제로 지킨다.
    # 방언 검사 없이 그대로 두면 `alembic upgrade head`가 SQLite에서 `CREATE
    # EXTENSION`(PostgreSQL 전용 구문)에 걸려 항상 실패한다 — 즉 기본 SQLite
    # 배포에서는 어떤 리비전도 head까지 올라갈 수 없었다.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "policy_chunks",
        sa.Column("chunk_id", sa.String(128), primary_key=True),
        sa.Column("index_version", sa.String(64), nullable=False),
        sa.Column("doc_id", sa.String(128), nullable=False),
        sa.Column("policy_year", sa.Integer(), nullable=False),
        sa.Column("clause", sa.String(64), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("record_fields", sa.JSON(), nullable=False),
        sa.Column("subjects", sa.JSON(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("keywords", sa.JSON(), nullable=False),
        sa.Column("search_tokens", sa.Text(), nullable=False),
        sa.Column(
            "search_vector",
            TSVECTOR(),
            sa.Computed("to_tsvector('simple', search_tokens)", persisted=True),
            nullable=True,
        ),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
    )
    op.create_index("ix_policy_chunks_index_version", "policy_chunks", ["index_version"])
    op.create_index("ix_policy_chunks_policy_year", "policy_chunks", ["policy_year"])
    op.execute(
        "CREATE INDEX ix_policy_chunks_search_vector ON policy_chunks USING gin (search_vector)"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_table("policy_chunks")
