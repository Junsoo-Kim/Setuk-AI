from __future__ import annotations

import os

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Computed, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBEDDING_DIM = int(os.environ.get("SETUK_POLICY_EMBEDDING_DIM", "1024"))


class PolicyBase(DeclarativeBase):
    """`db.Base`와 별도의 메타데이터.

    `policy_chunks`는 PostgreSQL 전용(TSVECTOR·pgvector) 테이블이라 SQLite
    부트스트랩(`Database.create_all`)이 함께 만들려고 하면 컴파일 오류가 난다.
    Alembic(0006)으로만 만든다."""


class PolicyChunkRow(PolicyBase):
    __tablename__ = "policy_chunks"

    chunk_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    index_version: Mapped[str] = mapped_column(String(64), index=True)
    doc_id: Mapped[str] = mapped_column(String(128))
    policy_year: Mapped[int] = mapped_column(Integer, index=True)
    clause: Mapped[str | None] = mapped_column(String(64), default=None)
    title: Mapped[str] = mapped_column(Text)
    source_name: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text, default=None)
    page: Mapped[int | None] = mapped_column(Integer, default=None)
    severity: Mapped[str] = mapped_column(String(16))
    record_fields: Mapped[list[str]] = mapped_column(JSON)
    subjects: Mapped[list[str]] = mapped_column(JSON)
    text: Mapped[str] = mapped_column(Text)
    keywords: Mapped[list[str]] = mapped_column(JSON)
    search_tokens: Mapped[str] = mapped_column(Text)
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', search_tokens)", persisted=True)
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)


Index("ix_policy_chunks_search_vector", PolicyChunkRow.search_vector, postgresql_using="gin")
