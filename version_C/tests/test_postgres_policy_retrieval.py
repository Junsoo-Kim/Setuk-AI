"""PostgreSQL FTS + pgvector 기반 규정 검색 테스트.

`DATABASE_URL`(또는 `SETUK_TEST_POSTGRES_URL`)로 접속 가능한 PostgreSQL이 없으면
전체를 건너뛴다 — SQLite로 개발하는 로컬 환경에서는 이 파일이 실행되지 않는다.
"""

from __future__ import annotations

import os
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEST_DATABASE_URL = os.environ.get(
    "SETUK_TEST_POSTGRES_URL",
    "postgresql+psycopg://postgres:setuk@localhost:5433/setuk",
)


def _postgres_available() -> bool:
    try:
        import psycopg

        with psycopg.connect(TEST_DATABASE_URL.replace("+psycopg", ""), connect_timeout=2):
            return True
    except Exception:
        return False


POSTGRES_AVAILABLE = _postgres_available()


@unittest.skipUnless(POSTGRES_AVAILABLE, "PostgreSQL(+pgvector)에 접속할 수 없어 건너뜀")
class PostgresPolicyRetrieverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from version_C.db import Database

        cls.db = Database(TEST_DATABASE_URL)

    @classmethod
    def tearDownClass(cls):
        cls.db.engine.dispose()

    def _make_chunks(self):
        from version_C.policy.models import PolicyChunk

        return [
            PolicyChunk(
                chunk_id="test:forbidden:language_test",
                doc_id="test-doc",
                policy_year=2026,
                clause="3항-가",
                title="공인어학시험 성적 기재 금지",
                source_name="테스트 발췌본",
                severity="block",
                text="토익, 토플 등 공인어학시험 성적을 기재할 수 없다.",
                keywords=["토익", "토플", "공인어학시험"],
            ),
            PolicyChunk(
                chunk_id="test:forbidden:mock_exam_score",
                doc_id="test-doc",
                policy_year=2026,
                clause="3항-나",
                title="모의고사 점수 기재 금지",
                source_name="테스트 발췌본",
                severity="block",
                text="모의고사 성적이나 전국연합학력평가 점수를 기재할 수 없다.",
                keywords=["모의고사", "점수", "성적"],
            ),
            PolicyChunk(
                chunk_id="test:forbidden:contest_award",
                doc_id="test-doc",
                policy_year=2025,
                clause="2항-다",
                title="교외 대회 수상실적 기재 금지",
                source_name="테스트 발췌본",
                severity="block",
                text="교외 대회의 수상 실적이나 참가 사실을 기재할 수 없다.",
                keywords=["대회", "수상"],
            ),
        ]

    def _build_retriever(self, index_version: str, **kwargs):
        from version_C.policy.postgres_retrieval import PostgresPolicyRetriever

        return PostgresPolicyRetriever(self.db, self._make_chunks(), index_version, **kwargs)

    def tearDown(self):
        from sqlalchemy import delete

        from version_C.policy.db_models import PolicyChunkRow

        with self.db.session() as session:
            session.execute(
                delete(PolicyChunkRow).where(
                    PolicyChunkRow.chunk_id.like("test:%")
                )
            )

    def test_exact_keyword_match_outranks_shared_vocabulary(self):
        """기획서 8.4의 '토익 점수' 사례: BM25만 쓰면 모의고사 조항이 위로 온다."""
        retriever = self._build_retriever(uuid.uuid4().hex)
        hits = retriever.search("토익 점수를 세특에 써도 되나", limit=3)
        self.assertTrue(hits)
        self.assertEqual(hits[0].chunk.chunk_id, "test:forbidden:language_test")
        self.assertEqual(hits[0].exact_rank, 0)

    def test_policy_year_filters_candidates(self):
        retriever = self._build_retriever(uuid.uuid4().hex)
        hits = retriever.search("대회 수상", limit=5, policy_year=2026)
        self.assertFalse(any(hit.chunk.chunk_id == "test:forbidden:contest_award" for hit in hits))

    def test_sparse_only_when_no_embedding_provider(self):
        retriever = self._build_retriever(uuid.uuid4().hex)
        self.assertFalse(retriever.is_hybrid)

    def test_rebuilding_with_a_new_index_version_removes_the_old_rows(self):
        from sqlalchemy import select

        from version_C.policy.db_models import PolicyChunkRow

        first_version = uuid.uuid4().hex
        self._build_retriever(first_version)
        with self.db.session() as session:
            count = len(
                session.scalars(
                    select(PolicyChunkRow).where(PolicyChunkRow.index_version == first_version)
                ).all()
            )
        self.assertEqual(count, 3)

        second_version = uuid.uuid4().hex
        self._build_retriever(second_version)
        with self.db.session() as session:
            stale = session.scalars(
                select(PolicyChunkRow).where(PolicyChunkRow.index_version == first_version)
            ).all()
        self.assertEqual(stale, [])

    def test_dense_ranking_uses_cosine_distance_when_embeddings_present(self):
        """실제 마이그레이션이 고정한 차원(1024)에 맞춰, 서로 직교하는 더미 벡터로
        코사인 거리 순위만 확인한다 — 임베딩 품질이 아니라 SQL 질의가 맞는지가 목적."""
        from version_C.policy.postgres_retrieval import PostgresPolicyRetriever

        dim = 1024
        chunks = self._make_chunks()

        def unit_vector(active_index: int) -> list[float]:
            vector = [0.0] * dim
            vector[active_index] = 1.0
            return vector

        vectors = [unit_vector(0), unit_vector(1), unit_vector(2)]

        def fake_embed(texts):
            return [unit_vector(0) for _ in texts]

        retriever = PostgresPolicyRetriever(
            self.db, chunks, uuid.uuid4().hex, embed=fake_embed, chunk_vectors=vectors
        )
        self.assertTrue(retriever.is_hybrid)
        hits = retriever.search("아무 질의", limit=1)
        self.assertTrue(hits)
        self.assertEqual(hits[0].chunk.chunk_id, "test:forbidden:language_test")


if __name__ == "__main__":
    unittest.main()
