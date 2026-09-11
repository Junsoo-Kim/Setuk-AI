"""PostgreSQL FTS + pgvector 기반 규정 검색.

`retrieval.PolicyRetriever`(순수 파이썬 BM25)와 같은 인터페이스(`search`, `is_hybrid`)를
구현한다. `DATABASE_URL`이 PostgreSQL일 때 `PolicyService`가 이쪽을 쓴다 — SQLite
배포(교사 로컬 PC)는 여전히 `PolicyRetriever`를 쓴다.

색인 텍스트는 `retrieval.tokenize`(한국어 접미사 제거 + 바이그램)를 그대로 재사용해
`search_tokens`에 저장한다. PostgreSQL 기본 `simple` 텍스트 검색 설정은 한국어 형태소를
모르므로, 색인·질의 양쪽에서 같은 토크나이저를 미리 거치지 않으면 조사만 다른 표현이
서로 매칭되지 않는다.
"""

from __future__ import annotations

from typing import Sequence

from sqlalchemy import delete, func, select, text

from ..db import Database
from .db_models import PolicyChunkRow
from .models import PolicyChunk, SearchHit
from .retrieval import RRF_K, _ranking, exact_keyword_scores, reciprocal_rank_fusion, tokenize


def _indexed_text(chunk: PolicyChunk) -> str:
    parts = [chunk.title, chunk.text, chunk.clause or "", " ".join(chunk.keywords)]
    return "\n".join(part for part in parts if part)


class PostgresPolicyRetriever:
    def __init__(
        self,
        db: Database,
        chunks: Sequence[PolicyChunk],
        index_version: str,
        embed=None,
        chunk_vectors: Sequence[Sequence[float]] | None = None,
    ):
        self.chunks = list(chunks)
        self._by_id = {chunk.chunk_id: chunk for chunk in self.chunks}
        self.db = db
        self._embed = embed
        self._sync(index_version, chunk_vectors)

    def _sync(self, index_version: str, chunk_vectors: Sequence[Sequence[float]] | None) -> None:
        """코퍼스를 `policy_chunks` 테이블과 맞춘다.

        다른 index_version(이전 코퍼스 빌드)의 행은 지운다 — 규정이 개정되면 옛 조항이
        검색에 남아 있으면 안 된다. 같은 chunk_id는 upsert한다.
        """
        with self.db.session() as session:
            session.execute(
                delete(PolicyChunkRow).where(PolicyChunkRow.index_version != index_version)
            )
            for position, chunk in enumerate(self.chunks):
                vector = list(chunk_vectors[position]) if chunk_vectors else None
                values = dict(
                    index_version=index_version,
                    doc_id=chunk.doc_id,
                    policy_year=chunk.policy_year,
                    clause=chunk.clause,
                    title=chunk.title,
                    source_name=chunk.source_name,
                    source_url=chunk.source_url,
                    page=chunk.page,
                    severity=chunk.severity,
                    record_fields=list(chunk.record_fields),
                    subjects=list(chunk.subjects),
                    text=chunk.text,
                    keywords=list(chunk.keywords),
                    search_tokens=" ".join(tokenize(_indexed_text(chunk))),
                    embedding=vector,
                )
                row = session.get(PolicyChunkRow, chunk.chunk_id)
                if row is None:
                    session.add(PolicyChunkRow(chunk_id=chunk.chunk_id, **values))
                else:
                    for key, value in values.items():
                        setattr(row, key, value)

    @property
    def is_hybrid(self) -> bool:
        return self._embed is not None

    def search(
        self,
        query: str,
        limit: int = 5,
        *,
        policy_year: int | None = None,
        record_field: str | None = None,
    ) -> list[SearchHit]:
        candidates = [
            chunk
            for chunk in self.chunks
            if (policy_year is None or chunk.policy_year == policy_year)
            and chunk.applies_to(record_field)
        ]
        if not candidates:
            return []
        allowed_ids = {chunk.chunk_id for chunk in candidates}
        pool = max(limit * 4, 20)

        exact_scores = exact_keyword_scores(query, candidates)
        exact_ranking = [
            candidates[i].chunk_id for i in _ranking(exact_scores, pool)
        ]

        sparse_ranking = self._sparse_ranking(query, allowed_ids, pool)
        dense_ranking = self._dense_ranking(query, allowed_ids, pool)

        rankings = [r for r in (exact_ranking, sparse_ranking, dense_ranking) if r]
        if not rankings:
            return []
        fused = reciprocal_rank_fusion(rankings, k=RRF_K)

        exact_positions = {cid: rank for rank, cid in enumerate(exact_ranking)}
        sparse_positions = {cid: rank for rank, cid in enumerate(sparse_ranking)}
        dense_positions = {cid: rank for rank, cid in enumerate(dense_ranking)}

        ordered = sorted(fused.items(), key=lambda item: (-item[1], item[0]))
        return [
            SearchHit(
                chunk=self._by_id[chunk_id],
                score=round(score, 6),
                exact_rank=exact_positions.get(chunk_id),
                sparse_rank=sparse_positions.get(chunk_id),
                dense_rank=dense_positions.get(chunk_id),
            )
            for chunk_id, score in ordered[:limit]
            if chunk_id in self._by_id
        ]

    def _sparse_ranking(self, query: str, allowed_ids: set[str], pool: int) -> list[str]:
        query_tokens = " | ".join(dict.fromkeys(tokenize(query)))
        if not query_tokens:
            return []
        with self.db.session() as session:
            rows = session.execute(
                select(PolicyChunkRow.chunk_id)
                .where(
                    PolicyChunkRow.chunk_id.in_(allowed_ids),
                    PolicyChunkRow.search_vector.op("@@")(
                        func.to_tsquery("simple", query_tokens)
                    ),
                )
                .order_by(
                    func.ts_rank_cd(
                        PolicyChunkRow.search_vector, func.to_tsquery("simple", query_tokens)
                    ).desc()
                )
                .limit(pool)
            ).all()
        return [row[0] for row in rows]

    def _dense_ranking(self, query: str, allowed_ids: set[str], pool: int) -> list[str]:
        if self._embed is None:
            return []
        try:
            query_vector = self._embed([query])[0]
        except Exception:  # 임베딩 제공자 장애가 규정 검색 전체를 막으면 안 된다
            return []
        with self.db.session() as session:
            rows = session.execute(
                select(PolicyChunkRow.chunk_id)
                .where(
                    PolicyChunkRow.chunk_id.in_(allowed_ids),
                    PolicyChunkRow.embedding.is_not(None),
                )
                .order_by(PolicyChunkRow.embedding.cosine_distance(query_vector))
                .limit(pool)
            ).all()
        return [row[0] for row in rows]
