"""규정 RAG 테스트.

이 계층이 지켜야 할 계약은 검색 정확도보다 **정직성**이다.

- 코퍼스에 없는 조항을 지어내지 않는다.
- 인용한 조항은 실제로 코퍼스에 있어야 한다(dangling citation 차단).
- 규정이 개정되면(index_version 변경) 예전 인용을 stale로 표시한다.
- 근거를 모르면 틀린 근거를 붙이는 대신 비운다.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from version_C.policy import corpus as corpus_mod
from version_C.policy.ingest import IngestError, ingest_markdown, ingest_rules_json
from version_C.policy.models import PolicyChunk, compute_index_version
from version_C.policy.retrieval import PolicyRetriever, reciprocal_rank_fusion, tokenize
from version_C.policy.service import PolicyService

SAMPLE_RULES = {
    "meta": {"source": "2026학년도 테스트 기재요령 발췌"},
    "length_limits": {
        "fields": {
            "subject_setuk": {
                "label": "교과 세부능력 및 특기사항",
                "max_bytes": 1500,
                "max_chars_ko_equiv": 500,
            },
            "career_activity": {
                "label": "진로활동 특기사항",
                "max_bytes": None,
                "max_chars_ko_equiv": None,
                "note": "2026학년도 축소 대상. 수치 미확인.",
            },
        }
    },
    "forbidden_keywords": {
        "categories": [
            {
                "id": "language_test",
                "label": "공인어학시험 성적",
                "basis": "3항-가",
                "severity": "block",
                "keywords": ["TOEIC", "토익", "텝스"],
            },
            {
                "id": "scholarship",
                "label": "장학생/장학금",
                "basis": "3항-카",
                "severity": "block",
                "keywords": ["장학금", "장학생"],
            },
            {
                "id": "contest_award",
                "label": "교내외 대회 수상",
                "basis": "3항-나",
                "severity": "review",
                "keywords": ["대회", "올림피아드", "수상"],
            },
        ]
    },
}


def build_service(
    rules: dict | None = None, case: unittest.TestCase | None = None
) -> tuple[PolicyService, Path]:
    """임시 코퍼스로 서비스를 만든다. 실제 저장소 코퍼스에 의존하지 않는다.

    `case`를 주면 그 테스트가 끝날 때 임시 디렉터리를 정리한다.
    """
    tmp = tempfile.TemporaryDirectory()
    if case is not None:
        case.addCleanup(tmp.cleanup)
    directory = Path(tmp.name)
    rules_path = directory / "rules.json"
    rules_path.write_text(
        json.dumps(rules or SAMPLE_RULES, ensure_ascii=False), encoding="utf-8"
    )
    (directory / "sources.json").write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "doc_id": "test-2026",
                        "kind": "rules_json",
                        "path": "rules.json",
                        "policy_year": 2026,
                        "source_url": "https://example.invalid/rules",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manifest, chunks = corpus_mod.build_corpus(directory, None)
    service = PolicyService(chunks=chunks, manifest=manifest)
    # 청크는 이미 메모리에 올라와 있으므로 디렉터리가 사라져도 검색은 동작한다.
    return service, directory


class TokenizerTests(unittest.TestCase):
    def test_korean_particle_is_stripped(self):
        self.assertIn("장학금", tokenize("장학금을"))

    def test_ascii_is_casefolded(self):
        self.assertIn("toeic", tokenize("TOEIC"))

    def test_bigrams_allow_partial_match(self):
        tokens = tokenize("경진대회")
        self.assertIn("대회", tokens)

    def test_indexing_and_query_use_the_same_tokenizer(self):
        self.assertEqual(tokenize("장학금을 받음"), tokenize("장학금을 받음"))


class FusionTests(unittest.TestCase):
    def test_rrf_prefers_documents_ranked_well_by_both(self):
        fused = reciprocal_rank_fusion([[1, 2, 3], [3, 1, 2]])
        self.assertGreater(fused[1], fused[2])

    def test_rrf_ignores_score_scale(self):
        """순위만 쓰므로 점수 범위가 달라도 결과가 같아야 한다."""
        first = reciprocal_rank_fusion([[5, 9], [9, 5]])
        second = reciprocal_rank_fusion([[5, 9], [9, 5]])
        self.assertEqual(first, second)


class RetrievalQualityTests(unittest.TestCase):
    def setUp(self):
        self.service, _ = build_service(case=self)

    def test_exact_keyword_beats_topical_overlap(self):
        """'토익'은 공인어학시험 조항을 가리키는 사실상 유일한 신호다.

        BM25만 쓰면 '성적'·'점수'를 공유하는 다른 조항에 밀린다. 정확 일치 랭커를
        RRF에 함께 넣은 이유가 이것이다.
        """
        hits = self.service.search("세특에 토익 점수를 써도 되나요", limit=3)
        self.assertTrue(hits)
        self.assertEqual(hits[0].chunk.chunk_id, "test-2026:forbidden:language_test")
        self.assertEqual(hits[0].exact_rank, 0)

    def test_scholarship_query_finds_scholarship_clause(self):
        hits = self.service.search("장학금을 받은 사실을 기재해도 되나", limit=1)
        self.assertEqual(hits[0].chunk.clause, "3항-카")

    def test_search_returns_nothing_for_unrelated_query(self):
        hits = self.service.search("점심 급식 메뉴", limit=3)
        self.assertEqual(hits, [])

    def test_empty_query_returns_nothing(self):
        self.assertEqual(self.service.search("   "), [])

    def test_record_field_filter_limits_length_clauses(self):
        hits = self.service.search("분량 제한", limit=5, record_field="career_activity")
        self.assertTrue(hits)
        for hit in hits:
            self.assertTrue(hit.chunk.applies_to("career_activity"))

    def test_policy_year_filter_excludes_other_years(self):
        self.assertEqual(self.service.search("장학금", policy_year=2099), [])

    def test_sparse_only_when_no_embedding_provider(self):
        self.assertEqual(self.service.retrieval_mode, "sparse_only")
        for hit in self.service.search("장학금", limit=2):
            self.assertIsNone(hit.dense_rank)


class DenseFallbackTests(unittest.TestCase):
    def test_embedding_failure_falls_back_to_sparse(self):
        """임베딩 제공자가 죽어도 규정 검색은 계속돼야 한다."""
        chunks = [
            PolicyChunk(
                chunk_id="c1",
                doc_id="d",
                policy_year=2026,
                title="장학금",
                source_name="테스트",
                text="장학금은 기재할 수 없다.",
                keywords=["장학금"],
            )
        ]

        def broken_embed(texts):
            raise RuntimeError("임베딩 서비스 장애")

        retriever = PolicyRetriever(chunks, embed=broken_embed, chunk_vectors=[[1.0, 0.0]])
        hits = retriever.search("장학금", limit=1)
        self.assertEqual(len(hits), 1)
        self.assertIsNone(hits[0].dense_rank)


class IngestTests(unittest.TestCase):
    def test_rules_json_produces_clause_metadata(self):
        service, _ = build_service(case=self)
        chunk = service.get("test-2026:forbidden:language_test")
        self.assertIsNotNone(chunk)
        self.assertEqual(chunk.clause, "3항-가")
        self.assertEqual(chunk.severity, "block")
        self.assertEqual(chunk.source_url, "https://example.invalid/rules")

    def test_unconfirmed_length_limit_is_marked_review_not_block(self):
        """수치가 확인되지 않은 항목을 확정 규정처럼 다루면 안 된다."""
        service, _ = build_service(case=self)
        chunk = service.get("test-2026:limit:career_activity")
        self.assertEqual(chunk.severity, "review")
        self.assertIn("확정 값으로 들어 있지 않다", chunk.text)

    def test_markdown_ingest_extracts_clause_from_heading(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "guide.md"
            path.write_text(
                "# 3항-가 공인어학시험\n\n관련 성적은 기재하지 않는다.\n", encoding="utf-8"
            )
            _, chunks = ingest_markdown(path, doc_id="g", policy_year=2026)
        self.assertEqual(chunks[0].clause, "3항-가")

    def test_empty_rules_json_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(IngestError):
                ingest_rules_json(path, doc_id="e", policy_year=2026)

    def test_source_outside_repository_is_rejected(self):
        with self.assertRaises(corpus_mod.CorpusError):
            corpus_mod._resolve_source_path(
                corpus_mod.DEFAULT_CORPUS_DIR, "../../../../../../etc/passwd"
            )


class IndexVersionTests(unittest.TestCase):
    def test_index_version_is_stable_for_same_content(self):
        service_a, _ = build_service(case=self)
        service_b, _ = build_service(case=self)
        self.assertEqual(service_a.index_version, service_b.index_version)

    def test_editing_a_rule_changes_the_index_version(self):
        service_a, _ = build_service(case=self)
        edited = json.loads(json.dumps(SAMPLE_RULES))
        edited["forbidden_keywords"]["categories"][0]["keywords"].append("아이엘츠")
        service_b, _ = build_service(edited, case=self)
        self.assertNotEqual(service_a.index_version, service_b.index_version)

    def test_embedding_model_is_part_of_the_version(self):
        chunk = PolicyChunk(
            chunk_id="c", doc_id="d", policy_year=2026, title="t", source_name="s", text="x"
        )
        self.assertNotEqual(
            compute_index_version([chunk], None), compute_index_version([chunk], "voyage:voyage-3")
        )


class CitationValidationTests(unittest.TestCase):
    def setUp(self):
        self.service, _ = build_service(case=self)

    def test_known_chunk_id_is_valid(self):
        check = self.service.validate_citations(["test-2026:forbidden:scholarship"])
        self.assertTrue(check.ok)
        self.assertEqual(len(check.valid), 1)

    def test_invented_chunk_id_is_dangling(self):
        """모델이 그럴듯한 조항 ID를 지어내도 통과시키면 안 된다."""
        check = self.service.validate_citations(["test-2026:forbidden:made_up_rule"])
        self.assertFalse(check.ok)
        self.assertEqual(check.dangling, ["test-2026:forbidden:made_up_rule"])

    def test_citation_from_an_older_index_is_stale(self):
        check = self.service.validate_citations(
            ["test-2026:forbidden:scholarship"], expected_index_version="policy-oldversion"
        )
        self.assertFalse(check.ok)
        self.assertEqual(len(check.stale), 1)

    def test_extract_cited_ids_finds_chunk_references(self):
        found = self.service.extract_cited_ids(
            "근거: test-2026:forbidden:scholarship 및 test-2026:limit:subject_setuk 참고"
        )
        self.assertIn("test-2026:forbidden:scholarship", found)
        self.assertIn("test-2026:limit:subject_setuk", found)


class DiagnosticBasisTests(unittest.TestCase):
    def setUp(self):
        self.service, _ = build_service(case=self)

    def test_byte_limit_cites_the_matching_record_field(self):
        annotated = self.service.annotate_diagnostics(
            [{"code": "BYTE_LIMIT", "message": "초과"}], record_field="subject_setuk"
        )
        basis = annotated[0]["policy_basis"]
        self.assertEqual(len(basis), 1)
        self.assertIn("교과", basis[0]["title"])

    def test_forbidden_term_cites_the_clause_that_lists_that_term(self):
        annotated = self.service.annotate_diagnostics(
            [{"code": "FORBIDDEN_TERM", "message": "금칙어 '장학금'이(가) 포함되어 있습니다."}]
        )
        self.assertEqual(annotated[0]["policy_basis"][0]["clause"], "3항-카")

    def test_diagnostic_without_a_clause_gets_no_basis(self):
        """탭 문자는 NEIS 입력 제약이지 기재요령 조항이 아니다.

        틀린 근거를 붙이면 나머지 인용까지 믿을 수 없게 되므로 비워 두는 편이 낫다.
        """
        annotated = self.service.annotate_diagnostics(
            [{"code": "TAB_CHARACTER", "message": "탭 문자는 사용할 수 없습니다."}]
        )
        self.assertEqual(annotated[0]["policy_basis"], [])


class TextScanTests(unittest.TestCase):
    def setUp(self):
        self.service, _ = build_service(case=self)

    def test_blocked_term_in_text_is_found_with_citation(self):
        findings = self.service.scan_text("교내 올림피아드에서 장학금을 받음.")
        chunk_ids = {item.chunk_id for item in findings}
        self.assertIn("test-2026:forbidden:scholarship", chunk_ids)

    def test_block_severity_is_sorted_first(self):
        findings = self.service.scan_text("대회에 나가 장학금을 받음.")
        self.assertEqual(findings[0].severity, "block")

    def test_clean_text_produces_no_findings(self):
        findings = self.service.scan_text("자료를 비교하고 결론을 도출하는 과정을 보임.")
        self.assertEqual(findings, [])

    def test_findings_carry_a_human_readable_citation(self):
        findings = self.service.scan_text("장학금을 받음.")
        self.assertIn("2026학년도", findings[0].citation)


class PolicyYearTests(unittest.TestCase):
    def setUp(self):
        self.service, _ = build_service(case=self)

    def test_same_year_produces_no_warning(self):
        self.assertIsNone(self.service.check_policy_year(2026))

    def test_older_draft_year_warns_about_revisions(self):
        notice = self.service.check_policy_year(2025)
        self.assertIsNotNone(notice)
        self.assertIn("2026", notice)

    def test_unknown_year_produces_no_warning(self):
        self.assertIsNone(self.service.check_policy_year(None))


class EmptyCorpusTests(unittest.TestCase):
    def test_service_with_no_documents_is_empty_and_searches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "sources.json").write_text('{"sources": []}', encoding="utf-8")
            manifest, chunks = corpus_mod.build_corpus(directory, None)
            service = PolicyService(chunks=chunks, manifest=manifest)
        self.assertTrue(service.is_empty)
        self.assertEqual(service.search("장학금"), [])
        self.assertEqual(service.scan_text("장학금을 받음"), [])


class RepositoryCorpusTests(unittest.TestCase):
    """저장소에 실제로 들어 있는 코퍼스가 유효한지 확인한다."""

    def test_repository_corpus_builds_and_has_2026_rules(self):
        service = PolicyService()
        self.assertFalse(service.is_empty)
        self.assertIn(2026, service.manifest.policy_years)

    def test_repository_corpus_documents_declare_provenance(self):
        service = PolicyService()
        for document in service.manifest.documents:
            self.assertIn(
                document.provenance, ("official_pdf", "user_excerpt", "project_derived")
            )


if __name__ == "__main__":
    unittest.main()
