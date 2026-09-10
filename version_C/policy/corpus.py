"""코퍼스 구성과 인덱스 버전 관리.

`sources.json`이 "어떤 문서를 어느 학년도로 넣을지"를 선언하고, 이 모듈이 그대로 읽어
청크를 만든 뒤 `index_version`을 계산한다. 문서를 바꾸거나 지우면 해시가 달라지므로
실행에 기록된 `index_version`과 대조해 "그때 그 규정"인지 판별할 수 있다.

빌드 결과는 `built/`에 캐시한다. 규정 문서는 학년도에 한 번 바뀌므로 매 서버 기동마다
PDF를 다시 파싱할 이유가 없다. 캐시가 원본과 어긋나면(원본 해시 불일치) 자동으로 다시 만든다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .ingest import IngestError, ingest_markdown, ingest_pdf, ingest_rules_json
from .models import CorpusManifest, DocumentRef, PolicyChunk, compute_index_version

DEFAULT_CORPUS_DIR = Path(__file__).resolve().parent.parent / "policy_corpus"
SOURCES_FILENAME = "sources.json"
CACHE_FILENAME = "index.json"


class CorpusError(Exception):
    pass


def _resolve_source_path(corpus_dir: Path, raw_path: str) -> Path:
    """원본 경로는 코퍼스 폴더 기준 상대 경로다.

    저장소 밖(`../../`)을 가리키는 선언을 허용한다 — `version_A/rules.json`처럼 다른
    버전 폴더의 파일을 가져와야 하기 때문이다. 다만 저장소 루트 밖으로는 못 나간다.
    """
    repo_root = corpus_dir.parent.parent
    candidate = (corpus_dir / raw_path).resolve()
    try:
        candidate.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise CorpusError(
            f"규정 원본은 저장소 안에 있어야 합니다: {raw_path}"
        ) from exc
    return candidate


def load_sources(corpus_dir: Path) -> list[dict[str, Any]]:
    path = corpus_dir / SOURCES_FILENAME
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CorpusError(f"{SOURCES_FILENAME}을 읽을 수 없습니다: {exc}") from exc
    sources = raw.get("sources") if isinstance(raw, dict) else raw
    if not isinstance(sources, list):
        raise CorpusError(f"{SOURCES_FILENAME}의 sources는 배열이어야 합니다.")
    return [item for item in sources if isinstance(item, dict) and item.get("enabled", True)]


def build_corpus(
    corpus_dir: Path | None = None, embedding_model: str | None = None
) -> tuple[CorpusManifest, list[PolicyChunk]]:
    """선언된 원본을 전부 읽어 청크와 매니페스트를 만든다.

    원본 하나가 실패해도 나머지는 살린다. 규정 검색이 전부 죽는 것보다 일부라도 되는
    편이 낫고, 실패는 매니페스트가 아니라 예외 메시지로 드러난다.
    """
    directory = corpus_dir or DEFAULT_CORPUS_DIR
    documents: list[DocumentRef] = []
    chunks: list[PolicyChunk] = []
    failures: list[str] = []

    for source in load_sources(directory):
        kind = source.get("kind")
        doc_id = source.get("doc_id")
        policy_year = source.get("policy_year")
        if not doc_id or not isinstance(policy_year, int):
            failures.append(f"doc_id와 policy_year가 필요합니다: {source}")
            continue
        try:
            path = _resolve_source_path(directory, source.get("path", ""))
            if not path.is_file():
                failures.append(f"원본 파일이 없습니다: {path}")
                continue
            common = {
                "doc_id": doc_id,
                "policy_year": policy_year,
                "source_url": source.get("source_url"),
            }
            if kind == "rules_json":
                reference, produced = ingest_rules_json(path, **common)
            elif kind == "markdown":
                reference, produced = ingest_markdown(
                    path,
                    source_name=source.get("source_name"),
                    provenance=source.get("provenance", "user_excerpt"),
                    **common,
                )
            elif kind == "pdf":
                reference, produced = ingest_pdf(
                    path, source_name=source.get("source_name"), **common
                )
            else:
                failures.append(f"알 수 없는 kind입니다: {kind!r}")
                continue
        except (IngestError, CorpusError) as exc:
            failures.append(str(exc))
            continue

        documents.append(reference)
        chunks.extend(produced)

    manifest = CorpusManifest(
        index_version=compute_index_version(chunks, embedding_model),
        embedding_model=embedding_model,
        retrieval_mode="hybrid" if embedding_model else "sparse_only",
        documents=documents,
    )
    if failures and not chunks:
        raise CorpusError("규정 코퍼스를 만들지 못했습니다:\n" + "\n".join(failures))
    return manifest, chunks


def save_cache(corpus_dir: Path, manifest: CorpusManifest, chunks: list[PolicyChunk]) -> Path:
    target = corpus_dir / "built"
    target.mkdir(parents=True, exist_ok=True)
    path = target / CACHE_FILENAME
    payload = {
        "manifest": manifest.model_dump(mode="json"),
        "chunks": [chunk.model_dump(mode="json") for chunk in chunks],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n"
    )
    return path


def load_cache(corpus_dir: Path) -> tuple[CorpusManifest, list[PolicyChunk]] | None:
    path = corpus_dir / "built" / CACHE_FILENAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        manifest = CorpusManifest.model_validate(payload["manifest"])
        chunks = [PolicyChunk.model_validate(item) for item in payload["chunks"]]
    except Exception:
        # 캐시가 깨졌으면 조용히 버리고 다시 만든다. 캐시는 성능 장치일 뿐이다.
        return None
    return manifest, chunks


def load_or_build(
    corpus_dir: Path | None = None,
    embedding_model: str | None = None,
    *,
    refresh: bool = False,
) -> tuple[CorpusManifest, list[PolicyChunk]]:
    """캐시가 현재 원본과 같은 버전이면 재사용하고, 아니면 다시 만든다."""
    directory = corpus_dir or DEFAULT_CORPUS_DIR
    if not refresh:
        cached = load_cache(directory)
        if cached is not None:
            manifest, chunks = cached
            expected = compute_index_version(chunks, embedding_model)
            if manifest.index_version == expected:
                return manifest, chunks

    manifest, chunks = build_corpus(directory, embedding_model)
    if chunks:
        save_cache(directory, manifest, chunks)
    return manifest, chunks
