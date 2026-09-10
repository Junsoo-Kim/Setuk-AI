"""규정 원문을 검색 가능한 청크로 바꾸는 수집기.

세 가지 입력을 받는다.

1. `version_A/rules.json` — 사용자가 제공한 2026학년도 기재요령 발췌본을 구조화한 파일.
   조항 번호(`basis`: "3항-가")와 severity가 이미 붙어 있어 그대로 조항 단위 청크가 된다.
2. Markdown — 교육청 안내나 Q&A처럼 PDF가 아닌 자료.
3. PDF — 교육부 기재요령 원문. PyMuPDF로 **페이지 번호를 보존하며** 추출한다.
   인용에 페이지가 찍혀야 교사가 원문에서 확인할 수 있기 때문이다.

셋 다 같은 `PolicyChunk`로 수렴하므로 검색·인용 코드는 출처를 구분하지 않는다.
구분이 필요한 곳은 신뢰도뿐이며 `DocumentRef.provenance`에 남는다.

**중요**: 이 모듈은 규정 문장을 만들어 내지 않는다. 입력 파일에 있는 내용만 옮긴다.
코퍼스가 비어 있으면 검색도 빈 결과를 내야지, 그럴듯한 규정을 지어내면 안 된다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import DocumentRef, PolicyChunk, RecordField, Severity, text_hash

# rules.json의 length_limits.fields 키가 곧 기재 항목 이름이다.
_KNOWN_RECORD_FIELDS: set[str] = {
    "subject_setuk",
    "individual_setuk",
    "autonomous_activity",
    "club_activity",
    "career_activity",
    "behavior_comment",
}

_SEVERITY_MAP: dict[str, Severity] = {"block": "block", "review": "review", "info": "info"}


class IngestError(Exception):
    pass


def _severity(value: Any) -> Severity:
    return _SEVERITY_MAP.get(str(value or "").strip().casefold(), "info")


def _record_fields(value: Any) -> list[RecordField]:
    if not value:
        return ["all"]
    fields = [item for item in value if item in _KNOWN_RECORD_FIELDS]
    return fields or ["all"]  # type: ignore[return-value]


def ingest_rules_json(
    path: Path,
    *,
    doc_id: str,
    policy_year: int,
    source_url: str | None = None,
) -> tuple[DocumentRef, list[PolicyChunk]]:
    """구조화된 rules.json을 조항 단위 청크로 변환한다.

    `version_A/rules.json`이 이 형태다. 금칙 항목마다 `basis`(조항)와 `severity`가
    있으므로, 검색 결과가 곧 "왜 안 되는지 + 어느 조항인지"가 된다.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IngestError(f"규정 JSON을 읽을 수 없습니다: {path} ({exc})") from exc
    if not isinstance(raw, dict):
        raise IngestError("규정 JSON의 최상위 값은 객체여야 합니다.")

    meta = raw.get("meta") or {}
    source_name = meta.get("source") or path.name
    chunks: list[PolicyChunk] = []

    # --- 항목별 분량 제한 ---
    limits = (raw.get("length_limits") or {}).get("fields") or {}
    for field_key, limit in limits.items():
        if not isinstance(limit, dict):
            continue
        label = limit.get("label") or field_key
        max_bytes = limit.get("max_bytes")
        note = (limit.get("note") or "").strip()
        if max_bytes:
            body = f"{label}의 최대 분량은 {max_bytes}바이트({limit.get('max_chars_ko_equiv')}자)이다."
        else:
            body = f"{label}의 최대 분량이 이 코퍼스에 확정 값으로 들어 있지 않다."
        if note:
            body += f" {note}"
        chunks.append(
            PolicyChunk(
                chunk_id=f"{doc_id}:limit:{field_key}",
                doc_id=doc_id,
                policy_year=policy_year,
                clause=limit.get("basis"),
                title=f"분량 제한 - {label}",
                source_name=source_name,
                source_url=source_url,
                severity="block" if max_bytes else "review",
                record_fields=_record_fields([field_key]),
                text=body,
                keywords=[label, field_key, "분량", "바이트", "글자수"],
            )
        )

    # --- 금칙 항목 ---
    for category in (raw.get("forbidden_keywords") or {}).get("categories") or []:
        if not isinstance(category, dict):
            continue
        category_id = category.get("id") or text_hash(json.dumps(category, sort_keys=True))[:8]
        label = category.get("label") or category_id
        keywords = [str(item) for item in (category.get("keywords") or [])]
        note = (category.get("note") or "").strip()
        body = f"{label}은(는) 학교생활기록부 기재 제한 대상이다."
        if note:
            body += f" {note}"
        if keywords:
            body += " 관련 표현: " + ", ".join(keywords[:30]) + "."
        chunks.append(
            PolicyChunk(
                chunk_id=f"{doc_id}:forbidden:{category_id}",
                doc_id=doc_id,
                policy_year=policy_year,
                clause=category.get("basis"),
                title=f"기재 제한 - {label}",
                source_name=source_name,
                source_url=source_url,
                severity=_severity(category.get("severity")),
                record_fields=_record_fields(category.get("record_fields")),
                text=body,
                keywords=keywords,
            )
        )

    # --- 정규식 기반 항목 ---
    for pattern in (raw.get("regex_patterns") or {}).get("patterns") or []:
        if not isinstance(pattern, dict):
            continue
        pattern_id = pattern.get("id") or text_hash(str(pattern))[:8]
        label = pattern.get("label") or pattern_id
        note = (pattern.get("note") or "").strip()
        body = f"{label}은(는) 기재 시 확인이 필요하다."
        if note:
            body += f" {note}"
        chunks.append(
            PolicyChunk(
                chunk_id=f"{doc_id}:pattern:{pattern_id}",
                doc_id=doc_id,
                policy_year=policy_year,
                clause=pattern.get("basis"),
                title=f"기재 주의 - {label}",
                source_name=source_name,
                source_url=source_url,
                severity=_severity(pattern.get("severity")),
                text=body,
                keywords=[str(item) for item in (pattern.get("keywords_fallback") or [])] + [label],
            )
        )

    # --- 하네스 자체 규칙(교육부 조항이 아님을 제목에 드러낸다) ---
    harness = raw.get("harness_specific") or {}
    for key, entry in harness.items():
        if not isinstance(entry, dict) or "keywords" not in entry:
            continue
        label = entry.get("label") or key
        note = (entry.get("note") or "").strip()
        body = f"{label}. 교육부 조항이 아니라 이 프로젝트가 산출물 품질을 위해 두는 규칙이다."
        if note:
            body += f" {note}"
        chunks.append(
            PolicyChunk(
                chunk_id=f"{doc_id}:harness:{key}",
                doc_id=doc_id,
                policy_year=policy_year,
                clause=None,
                title=f"하네스 규칙 - {label}",
                source_name=source_name,
                source_url=source_url,
                severity=_severity(entry.get("severity")),
                text=body,
                keywords=[str(item) for item in entry.get("keywords", [])],
            )
        )

    if not chunks:
        raise IngestError(f"규정 JSON에서 청크를 하나도 만들지 못했습니다: {path}")

    reference = DocumentRef(
        doc_id=doc_id,
        policy_year=policy_year,
        source_name=source_name,
        source_url=source_url,
        ingested_from=str(path.name),
        content_hash=text_hash(path.read_text(encoding="utf-8-sig")),
        chunk_count=len(chunks),
        provenance="user_excerpt",
    )
    return reference, chunks


_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")
_CLAUSE_RE = re.compile(r"(\d+항(?:-[가-힣])?|제\s*\d+\s*조(?:\s*제?\s*\d+\s*항)?)")


def ingest_markdown(
    path: Path,
    *,
    doc_id: str,
    policy_year: int,
    source_name: str | None = None,
    source_url: str | None = None,
    provenance: str = "user_excerpt",
) -> tuple[DocumentRef, list[PolicyChunk]]:
    """제목 단위로 끊어 청크를 만든다. 제목에 조항 번호가 있으면 뽑아 둔다."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise IngestError(f"규정 Markdown을 읽을 수 없습니다: {path} ({exc})") from exc

    sections: list[tuple[str, list[str]]] = []
    current_title = path.stem
    buffer: list[str] = []
    for line in raw.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            if buffer:
                sections.append((current_title, buffer))
            current_title = heading.group(2).strip()
            buffer = []
        else:
            buffer.append(line)
    if buffer:
        sections.append((current_title, buffer))

    chunks = []
    for index, (title, lines) in enumerate(sections):
        body = "\n".join(lines).strip()
        if not body:
            continue
        clause_match = _CLAUSE_RE.search(title) or _CLAUSE_RE.search(body[:200])
        chunks.append(
            PolicyChunk(
                chunk_id=f"{doc_id}:md:{index:04d}",
                doc_id=doc_id,
                policy_year=policy_year,
                clause=clause_match.group(1) if clause_match else None,
                title=title,
                source_name=source_name or path.stem,
                source_url=source_url,
                text=body,
            )
        )
    if not chunks:
        raise IngestError(f"Markdown에서 본문을 찾지 못했습니다: {path}")

    reference = DocumentRef(
        doc_id=doc_id,
        policy_year=policy_year,
        source_name=source_name or path.stem,
        source_url=source_url,
        ingested_from=path.name,
        content_hash=text_hash(raw),
        chunk_count=len(chunks),
        provenance=provenance,  # type: ignore[arg-type]
    )
    return reference, chunks


def ingest_pdf(
    path: Path,
    *,
    doc_id: str,
    policy_year: int,
    source_name: str | None = None,
    source_url: str | None = None,
    min_chars: int = 80,
) -> tuple[DocumentRef, list[PolicyChunk]]:
    """교육부 기재요령 PDF 원문을 페이지 번호를 보존하며 청크로 만든다.

    페이지가 인용에 찍혀야 교사가 원문에서 직접 확인할 수 있다. 문단이 너무 짧으면
    (표 조각, 머리말) 노이즈이므로 `min_chars` 미만은 버린다.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover - 설치 환경에 따라 다름
        raise IngestError(
            "PDF를 읽으려면 pymupdf가 필요합니다: pip install -r requirements.lock.txt"
        ) from exc

    try:
        document = fitz.open(path)
    except Exception as exc:
        raise IngestError(f"PDF를 열 수 없습니다: {path} ({exc})") from exc

    chunks: list[PolicyChunk] = []
    hasher_input: list[str] = []
    try:
        for page_index in range(document.page_count):
            page_number = page_index + 1
            text = document.load_page(page_index).get_text("text")
            hasher_input.append(text)
            for block_index, block in enumerate(_split_blocks(text)):
                if len(block) < min_chars:
                    continue
                clause_match = _CLAUSE_RE.search(block[:200])
                chunks.append(
                    PolicyChunk(
                        chunk_id=f"{doc_id}:p{page_number:04d}:{block_index:02d}",
                        doc_id=doc_id,
                        policy_year=policy_year,
                        clause=clause_match.group(1) if clause_match else None,
                        title=_first_line(block),
                        source_name=source_name or path.stem,
                        source_url=source_url,
                        page=page_number,
                        text=block,
                    )
                )
    finally:
        document.close()

    if not chunks:
        raise IngestError(
            f"PDF에서 추출한 본문이 없습니다(스캔 이미지 PDF일 수 있습니다): {path}"
        )

    reference = DocumentRef(
        doc_id=doc_id,
        policy_year=policy_year,
        source_name=source_name or path.stem,
        source_url=source_url,
        ingested_from=path.name,
        content_hash=text_hash("\n".join(hasher_input)),
        chunk_count=len(chunks),
        provenance="official_pdf",
    )
    return reference, chunks


def _split_blocks(text: str) -> list[str]:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text)]
    return [block for block in blocks if block]


def _first_line(block: str) -> str:
    line = block.splitlines()[0].strip()
    return line[:80] if line else "(제목 없음)"
