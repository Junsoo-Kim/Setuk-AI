"""규정 검색 파사드와 인용 검증.

파이프라인·Linter·MCP 서버가 전부 이 클래스 하나만 쓴다.

세 가지를 책임진다.

1. **검색**: 질의 → 근거 조항(학년도·조항·페이지·출처 URL 포함)
2. **인용 검증**: 모델이나 도구가 내놓은 인용이 실제 코퍼스에 있는 조항인지 확인.
   없는 조항을 지어낸 인용은 "출처가 있다"는 인상만 주고 실제로는 확인 불가능하므로,
   검색만큼 중요하다.
3. **정책 버전 대조**: 이 초안이 만들어질 때의 학년도와 지금 코퍼스의 최신 학년도가
   다르면 경고한다. 2026학년도에 진로활동 분량이 축소된 것처럼, 작년 기준으로 통과한
   초안이 올해는 위반일 수 있다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from ..db import database_url
from .corpus import DEFAULT_CORPUS_DIR, load_or_build
from .models import CorpusManifest, PolicyChunk, SearchHit
from .retrieval import PolicyRetriever

# Linter의 FORBIDDEN_TERM 메시지에서 실제로 걸린 단어를 뽑는 패턴.
# linter.py 형식: "금칙어 '공인어학시험 점수'이(가) 포함되어 있습니다. ..."
_QUOTED_TERM_RE = re.compile(r"['‘“]([^'’”]{2,60})['’”]")


def _quoted_term(message: str) -> str | None:
    match = _QUOTED_TERM_RE.search(message)
    return match.group(1) if match else None


@dataclass
class PolicyFinding:
    """본문에서 발견한 규정 관련 사항. Linter 진단과 달리 통과/실패를 바꾸지 않는다."""

    severity: str
    chunk_id: str
    title: str
    citation: str
    matched: str
    clause: str | None = None
    source_url: str | None = None
    page: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "chunk_id": self.chunk_id,
            "title": self.title,
            "citation": self.citation,
            "matched": self.matched,
            "clause": self.clause,
            "source_url": self.source_url,
            "page": self.page,
        }


@dataclass
class CitationCheck:
    """인용 검증 결과."""

    valid: list[str] = field(default_factory=list)
    dangling: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.dangling and not self.stale


def embedding_provider() -> tuple[Callable[[list[str]], list[list[float]]] | None, str | None]:
    """설정된 임베딩 제공자를 돌려준다. 없으면 (None, None) — sparse-only로 동작한다.

    Anthropic은 임베딩 API를 제공하지 않으므로 dense 검색을 켜려면 키가 하나 더 필요하다.
    규정 검색은 조항 번호·금칙 용어처럼 정확 일치가 결정적인 질의가 많아 BM25만으로도
    실용적이므로 기본값은 끔이다.
    """
    provider = os.environ.get("SETUK_EMBEDDING_PROVIDER", "").strip().casefold()
    if not provider or provider == "none":
        return None, None

    if provider == "voyage":
        model = os.environ.get("SETUK_EMBEDDING_MODEL", "voyage-3")

        def embed_voyage(texts: list[str]) -> list[list[float]]:
            import voyageai

            client = voyageai.Client(api_key=os.environ["VOYAGE_API_KEY"])
            return client.embed(texts, model=model).embeddings

        return embed_voyage, f"voyage:{model}"

    if provider == "openai":
        model = os.environ.get("SETUK_EMBEDDING_MODEL", "text-embedding-3-small")

        def embed_openai(texts: list[str]) -> list[list[float]]:
            from openai import OpenAI

            client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
            response = client.embeddings.create(model=model, input=texts)
            return [item.embedding for item in response.data]

        return embed_openai, f"openai:{model}"

    raise ValueError(
        f"알 수 없는 SETUK_EMBEDDING_PROVIDER입니다: {provider!r} (none/voyage/openai)"
    )




class PolicyService:
    """규정 코퍼스 하나를 감싼 검색·검증 서비스."""

    def __init__(
        self,
        corpus_dir: Path | None = None,
        *,
        chunks: Sequence[PolicyChunk] | None = None,
        manifest: CorpusManifest | None = None,
        refresh: bool = False,
    ):
        embed, model = (None, None)
        try:
            embed, model = embedding_provider()
        except ValueError:
            # 설정 오류로 규정 검색 전체를 막지 않는다. sparse-only로 내려간다.
            embed, model = None, None

        if chunks is not None and manifest is not None:
            self.manifest, self.chunks = manifest, list(chunks)
        else:
            self.manifest, self.chunks = load_or_build(
                corpus_dir or DEFAULT_CORPUS_DIR, model, refresh=refresh
            )

        vectors = None
        if embed is not None and self.chunks:
            try:
                vectors = embed([chunk.text for chunk in self.chunks])
            except Exception:
                vectors = None  # 임베딩 실패 시 sparse-only 폴백

        if database_url().startswith("postgres"):
            from ..db import Database
            from .postgres_retrieval import PostgresPolicyRetriever

            self.retriever = PostgresPolicyRetriever(
                Database(), self.chunks, self.manifest.index_version,
                embed=embed, chunk_vectors=vectors,
            )
        else:
            self.retriever = PolicyRetriever(self.chunks, embed=embed, chunk_vectors=vectors)
        self._by_id = {chunk.chunk_id: chunk for chunk in self.chunks}

    # ------------------------------------------------------------------ 검색

    @property
    def is_empty(self) -> bool:
        return not self.chunks

    @property
    def index_version(self) -> str:
        return self.manifest.index_version

    @property
    def retrieval_mode(self) -> str:
        return "hybrid" if self.retriever.is_hybrid else "sparse_only"

    def search(
        self,
        query: str,
        limit: int = 5,
        *,
        policy_year: int | None = None,
        record_field: str | None = None,
    ) -> list[SearchHit]:
        if not query.strip():
            return []
        return self.retriever.search(
            query, limit=limit, policy_year=policy_year, record_field=record_field
        )

    def get(self, chunk_id: str) -> PolicyChunk | None:
        return self._by_id.get(chunk_id)

    # -------------------------------------------------------------- 본문 점검

    def scan_text(
        self, text: str, *, policy_year: int | None = None, record_field: str | None = None
    ) -> list[PolicyFinding]:
        """본문에서 코퍼스의 금칙 표현을 찾아 근거 조항과 함께 돌려준다.

        version_B의 Linter는 `rules.json`의 금칙어 2개만 본다. 코퍼스에는 사용자가 제공한
        2026학년도 발췌본의 금칙 항목 전체가 들어 있으므로, B의 검사 계약을 건드리지 않고
        **자문(advisory) 성격의 추가 점검**을 여기서 제공한다. 통과/실패는 바꾸지 않는다.
        """
        findings: list[PolicyFinding] = []
        folded = text.casefold()
        seen: set[tuple[str, str]] = set()

        for chunk in self.chunks:
            if policy_year is not None and chunk.policy_year != policy_year:
                continue
            if not chunk.applies_to(record_field):
                continue
            if chunk.severity not in ("block", "review"):
                continue
            for keyword in chunk.keywords:
                token = keyword.strip()
                # 한두 글자 키워드는 오탐이 너무 많다("대회"는 잡되 "상"은 버린다).
                if len(token) < 2:
                    continue
                if token.casefold() not in folded:
                    continue
                key = (chunk.chunk_id, token)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    PolicyFinding(
                        severity=chunk.severity,
                        chunk_id=chunk.chunk_id,
                        title=chunk.title,
                        citation=chunk.citation_label(),
                        matched=token,
                        clause=chunk.clause,
                        source_url=chunk.source_url,
                        page=chunk.page,
                    )
                )
                break  # 조항 하나당 한 번만 보고한다

        findings.sort(key=lambda item: (0 if item.severity == "block" else 1, item.chunk_id))
        return findings

    def basis_for_diagnostic(
        self, diagnostic: dict[str, Any], *, record_field: str | None = None
    ) -> list[SearchHit]:
        """진단 하나에 붙일 근거 조항을 고른다.

        진단 코드와 조항의 관계는 대부분 **결정적**이다. BYTE_LIMIT은 언제나 해당 기재
        항목의 분량 조항이고, FORBIDDEN_TERM은 그 단어를 금칙어로 가진 조항이다. 여기에
        의미 검색을 쓰면 "제한"·"기재" 같은 흔한 단어를 공유하는 엉뚱한 조항이 근거로
        붙는다(실제로 분량 진단에 '인증시험' 조항이 달렸다). 그래서 알 수 있는 것은
        직접 찾고, 검색은 남는 경우에만 쓴다.

        근거가 없으면 빈 목록을 돌려준다. **틀린 근거를 붙이는 것보다 없는 편이 낫다** —
        인용이 한 번 틀리면 나머지 인용도 믿을 수 없게 된다.
        """
        code = str(diagnostic.get("code") or "")

        if code in ("BYTE_LIMIT", "BELOW_TARGET_LENGTH"):
            return self._length_basis(record_field)

        if code == "FORBIDDEN_TERM":
            term = _quoted_term(str(diagnostic.get("message") or ""))
            if term:
                return self._keyword_basis(term, record_field)
            return []

        # TAB_CHARACTER·UNSUPPORTED_CHARACTER는 NEIS 입력 제약이지 기재요령 조항이 아니다.
        # EMPTY_CONTENT도 마찬가지다. 코퍼스에 근거가 없으므로 만들어 붙이지 않는다.
        return []

    def _length_basis(self, record_field: str | None) -> list[SearchHit]:
        target = record_field or "subject_setuk"
        return [
            SearchHit(chunk=chunk, score=1.0)
            for chunk in self.chunks
            if ":limit:" in chunk.chunk_id and target in chunk.record_fields
        ]

    def _keyword_basis(self, term: str, record_field: str | None) -> list[SearchHit]:
        folded = term.casefold()
        return [
            SearchHit(chunk=chunk, score=1.0)
            for chunk in self.chunks
            if chunk.applies_to(record_field)
            and any(keyword.casefold() == folded for keyword in chunk.keywords)
        ]

    def annotate_diagnostics(
        self,
        diagnostics: Sequence[dict[str, Any]],
        *,
        policy_year: int | None = None,
        record_field: str | None = None,
    ) -> list[dict[str, Any]]:
        """Linter 진단마다 근거 규정을 붙인다(계획서: '진단에 근거 규정과 페이지 표시')."""
        annotated = []
        for diagnostic in diagnostics:
            item = dict(diagnostic)
            hits = self.basis_for_diagnostic(item, record_field=record_field)
            if policy_year is not None:
                hits = [hit for hit in hits if hit.chunk.policy_year == policy_year]
            item["policy_basis"] = [hit.as_citation() for hit in hits[:2]]
            annotated.append(item)
        return annotated

    # -------------------------------------------------------------- 인용 검증

    def validate_citations(
        self, chunk_ids: Sequence[str], *, expected_index_version: str | None = None
    ) -> CitationCheck:
        """인용된 조항이 실제로 현재 코퍼스에 있는지 확인한다.

        `expected_index_version`이 지금 버전과 다르면, 존재하는 조항이라도 `stale`로
        분류한다 — 같은 chunk_id라도 내용이 개정되었을 수 있기 때문이다.
        """
        check = CitationCheck()
        outdated = (
            expected_index_version is not None
            and expected_index_version != self.manifest.index_version
        )
        for chunk_id in chunk_ids:
            if chunk_id not in self._by_id:
                check.dangling.append(chunk_id)
            elif outdated:
                check.stale.append(chunk_id)
            else:
                check.valid.append(chunk_id)
        return check

    def extract_cited_ids(self, text: str) -> list[str]:
        """본문에 섞인 chunk_id 형태의 인용을 뽑는다(도구 응답 검증용)."""
        return re.findall(r"\b[\w.-]+:(?:limit|forbidden|pattern|harness|md|p\d+):[\w.-]+\b", text)

    # ---------------------------------------------------------- 정책 버전 대조

    def check_policy_year(self, run_policy_year: int | None) -> str | None:
        """초안을 만든 학년도와 코퍼스 최신 학년도가 다르면 경고 문자열을 돌려준다."""
        latest = self.manifest.latest_policy_year
        if latest is None or run_policy_year is None:
            return None
        if run_policy_year == latest:
            return None
        if run_policy_year < latest:
            return (
                f"이 초안은 {run_policy_year}학년도 규정 기준으로 작성되었으나, 코퍼스에는 "
                f"{latest}학년도 규정이 들어 있습니다. 분량 상한과 기재 제한 항목이 개정되었을 수 "
                "있으니 최신 기재요령으로 다시 확인하세요."
            )
        return (
            f"이 초안은 {run_policy_year}학년도 기준으로 표시되어 있으나 코퍼스의 최신 규정은 "
            f"{latest}학년도입니다. 코퍼스를 갱신해야 할 수 있습니다."
        )

    def describe(self) -> dict[str, Any]:
        """검색 결과와 함께 보여 줄 코퍼스 상태."""
        return {
            "index_version": self.index_version,
            "retrieval_mode": self.retrieval_mode,
            "embedding_model": self.manifest.embedding_model,
            "chunk_count": len(self.chunks),
            "policy_years": self.manifest.policy_years,
            "documents": [
                {
                    "doc_id": doc.doc_id,
                    "policy_year": doc.policy_year,
                    "source_name": doc.source_name,
                    "provenance": doc.provenance,
                    "chunk_count": doc.chunk_count,
                }
                for doc in self.manifest.documents
            ],
        }


_SERVICE: PolicyService | None = None


def get_service(refresh: bool = False) -> PolicyService:
    """서버·MCP가 공유하는 단일 인스턴스. 코퍼스 빌드를 매번 반복하지 않는다."""
    global _SERVICE
    if _SERVICE is None or refresh:
        _SERVICE = PolicyService(refresh=refresh)
    return _SERVICE
