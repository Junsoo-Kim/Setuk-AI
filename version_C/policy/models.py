"""규정 코퍼스의 데이터 모델.

학교생활기록부 기재요령은 **학년도마다 개정된다.** 2026학년도에 진로활동·행동특성의
분량이 축소된 것처럼, 작년에 맞던 초안이 올해는 규정 위반이 될 수 있다. 그래서 이
코퍼스는 "규정 문서 하나"가 아니라 **학년도별 스냅샷의 집합**으로 설계했고, 모든
검색 결과는 어느 학년도 어느 조항에서 왔는지를 반드시 달고 나온다.

`index_version`은 코퍼스 내용 전체의 해시다. 문서를 고치거나 지우면 값이 바뀌므로,
실행(run)에 기록해 두면 "이 초안은 그때 그 규정으로 만든 것"이라고 나중에 증명할 수 있다.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["block", "review", "info"]

# 학교생활기록부의 기재 항목. 규정은 항목마다 다르게 적용된다.
RecordField = Literal[
    "subject_setuk",
    "individual_setuk",
    "autonomous_activity",
    "club_activity",
    "career_activity",
    "behavior_comment",
    "all",
]


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class PolicyChunk(BaseModel):
    """검색 단위 하나. 조항 하나 또는 문서의 한 문단."""

    model_config = ConfigDict(frozen=True)

    chunk_id: str
    doc_id: str

    # --- 출처 메타데이터 (검색 결과에 그대로 표시된다) ---
    policy_year: int = Field(description="시행 학년도. 2026 = 2026학년도 기재요령")
    clause: str | None = Field(default=None, description="조항 번호. 예: 3항-가")
    title: str
    source_name: str
    source_url: str | None = None
    page: int | None = None

    # --- 적용 범위 ---
    severity: Severity = "info"
    record_fields: list[RecordField] = Field(default_factory=lambda: ["all"])
    subjects: list[str] = Field(default_factory=list, description="비면 전 과목 적용")

    # --- 본문 ---
    text: str
    keywords: list[str] = Field(default_factory=list)

    @property
    def content_hash(self) -> str:
        return text_hash(f"{self.chunk_id}|{self.text}|{self.clause}|{self.policy_year}")

    def applies_to(self, record_field: str | None) -> bool:
        if record_field is None or "all" in self.record_fields:
            return True
        return record_field in self.record_fields

    def citation_label(self) -> str:
        """사람이 읽는 인용 문자열. 페이지가 있으면 함께 표시한다.

        문서 이름 자체에 이미 학년도가 들어 있는 경우(예: "2026학년도 학교생활기록부
        기재요령")가 흔하므로 그때는 연도를 앞에 덧붙이지 않는다.
        """
        year = f"{self.policy_year}학년도"
        head = self.source_name if year in self.source_name else f"{year} {self.source_name}"
        parts = [head]
        if self.clause:
            parts.append(self.clause)
        if self.page is not None:
            parts.append(f"p.{self.page}")
        return " ".join(parts)


class DocumentRef(BaseModel):
    """코퍼스에 들어 있는 원본 문서 하나."""

    doc_id: str
    policy_year: int
    source_name: str
    source_url: str | None = None
    ingested_from: str
    content_hash: str
    chunk_count: int
    # 사용자가 제공한 발췌본인지, 공식 원문 PDF인지 구분한다. 신뢰도가 다르다.
    provenance: Literal["official_pdf", "user_excerpt", "project_derived"] = "user_excerpt"


class CorpusManifest(BaseModel):
    """코퍼스 전체의 상태. 이 값이 곧 검색 결과의 재현 가능성이다."""

    index_version: str
    built_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    embedding_model: str | None = None
    retrieval_mode: Literal["hybrid", "sparse_only"] = "sparse_only"
    documents: list[DocumentRef] = Field(default_factory=list)

    @property
    def policy_years(self) -> list[int]:
        return sorted({doc.policy_year for doc in self.documents})

    @property
    def latest_policy_year(self) -> int | None:
        years = self.policy_years
        return years[-1] if years else None


def compute_index_version(chunks: list[PolicyChunk], embedding_model: str | None) -> str:
    """코퍼스 내용 + 임베딩 모델의 해시.

    임베딩 모델을 바꾸면 같은 문서라도 검색 결과가 달라지므로 버전에 포함한다.
    """
    digest = hashlib.sha256()
    for chunk in sorted(chunks, key=lambda item: item.chunk_id):
        digest.update(chunk.content_hash.encode("utf-8"))
    digest.update((embedding_model or "sparse-only").encode("utf-8"))
    return f"policy-{digest.hexdigest()[:12]}"


class SearchHit(BaseModel):
    """검색 결과 하나. 점수 출처를 나눠 두어 왜 뽑혔는지 설명할 수 있게 한다."""

    chunk: PolicyChunk
    score: float
    exact_rank: int | None = None
    sparse_rank: int | None = None
    dense_rank: int | None = None

    def as_citation(self) -> dict:
        return {
            "chunk_id": self.chunk.chunk_id,
            "citation": self.chunk.citation_label(),
            "policy_year": self.chunk.policy_year,
            "clause": self.chunk.clause,
            "title": self.chunk.title,
            "severity": self.chunk.severity,
            "source_url": self.chunk.source_url,
            "page": self.chunk.page,
        }
