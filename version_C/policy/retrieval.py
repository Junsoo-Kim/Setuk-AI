"""규정 검색: BM25 sparse + 선택적 dense, RRF로 병합.

## 왜 순수 파이썬 BM25인가

규정 코퍼스는 조항 수백 개 규모다. OpenSearch나 Elasticsearch를 띄우는 것은 이 크기에
맞지 않고, 교사 한 명이 로컬에서 돌리는 C버전에 검색 서버를 설치 조건으로 걸 수도 없다.
그래서 BM25를 직접 구현했다(~80줄). 덤으로 한국어 토크나이저를 검색과 인덱스 양쪽에서
정확히 같은 것으로 쓸 수 있다.

## 왜 dense를 선택 사항으로 두었나

Anthropic은 임베딩 API를 제공하지 않으므로 dense 검색을 켜려면 별도 제공자(Voyage,
OpenAI)의 키를 하나 더 받거나 로컬 모델을 내려받아야 한다. 규정 검색은 조항 번호,
금칙 용어, 항목명처럼 **정확히 일치하는 토큰**이 결정적인 질의가 대부분이라 BM25만으로도
실용적이다. 그래서 기본값은 sparse-only이고, 임베딩 제공자가 설정되면 자동으로
hybrid(RRF)로 올라간다. 어느 쪽으로 동작했는지는 `CorpusManifest.retrieval_mode`와
검색 결과의 `sparse_rank`/`dense_rank`에 남는다.

## 왜 가중합이 아니라 RRF인가

BM25 점수는 상한이 없고 코퍼스에 따라 범위가 변하지만 코사인 유사도는 [-1, 1]이다.
두 점수를 직접 가중합하면 코퍼스가 바뀔 때마다 가중치를 다시 튜닝해야 한다. RRF는
점수 대신 **순위만** 쓰므로 그 문제가 없다.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Callable, Iterable, Sequence

from .models import PolicyChunk, SearchHit

# RRF 상수. 원논문(Cormack et al., 2009) 권장값이며, 상위권 순위 차이를 과하게
# 벌리지 않으려는 완충 항이다.
RRF_K = 60

_TOKEN_RE = re.compile(r"[0-9A-Za-z]+|[가-힣]+")

# 조사·어미만 남는 토큰은 변별력이 없다. 형태소 분석기(konlpy 등)는 JVM이나 대용량
# 사전을 요구하므로, 규정 문서 수준에서는 접미 제거 + 음절 bigram으로 충분하다.
_KO_SUFFIXES = (
    "으로부터", "에서는", "에게서", "이라고", "으로써", "으로서",
    "에서", "에게", "께서", "부터", "까지", "보다", "처럼", "만큼", "이나", "라고",
    "으로", "하고", "이며", "이고", "하며", "지만", "면서",
    "은", "는", "이", "가", "을", "를", "의", "에", "와", "과", "도", "로", "만",
)

_MIN_KO_STEM = 2


def _strip_korean_suffix(token: str) -> str:
    for suffix in _KO_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= _MIN_KO_STEM:
            return token[: -len(suffix)]
    return token


def tokenize(text: str) -> list[str]:
    """검색과 인덱싱이 반드시 같은 함수를 쓴다.

    한글은 어절에서 조사를 떼어 낸 어간과, 부분 일치를 잡기 위한 음절 bigram을 함께
    낸다. "장학금을"과 "장학금", "교외대회"와 "대회"가 서로 걸리게 하기 위함이다.
    """
    tokens: list[str] = []
    for raw in _TOKEN_RE.findall(text):
        if raw.isascii():
            tokens.append(raw.casefold())
            continue
        stem = _strip_korean_suffix(raw)
        tokens.append(stem)
        if stem != raw:
            tokens.append(raw)
        if len(stem) >= 3:
            tokens.extend(stem[i : i + 2] for i in range(len(stem) - 1))
    return tokens


class BM25Index:
    """Okapi BM25. 문서 수가 적으므로 전체 스캔으로 충분하다.

    `b`(길이 정규화 강도)를 기본 0.75가 아니라 0.4로 낮춘다. 이 코퍼스의 "문서 길이"는
    대부분 금칙 키워드 목록의 길이인데, 키워드가 25개인 조항이 5개인 조항보다 덜
    구체적인 것은 아니기 때문이다. 0.75로 두면 키워드를 많이 나열한 조항일수록
    불리해져서, 정작 그 키워드로 검색했을 때 밀려나는 역효과가 난다.
    """

    def __init__(self, documents: Sequence[Sequence[str]], k1: float = 1.5, b: float = 0.4):
        self.k1 = k1
        self.b = b
        self.doc_count = len(documents)
        self.doc_frequencies: list[Counter[str]] = [Counter(doc) for doc in documents]
        self.doc_lengths = [len(doc) for doc in documents]
        self.average_length = (
            sum(self.doc_lengths) / self.doc_count if self.doc_count else 0.0
        )
        document_frequency: Counter[str] = Counter()
        for frequencies in self.doc_frequencies:
            document_frequency.update(frequencies.keys())
        # BM25+ 계열의 평활화된 IDF. 흔한 토큰이 음수 점수를 내지 않게 한다.
        self.idf = {
            term: math.log(1 + (self.doc_count - count + 0.5) / (count + 0.5))
            for term, count in document_frequency.items()
        }

    def scores(self, query_tokens: Sequence[str]) -> list[float]:
        results = [0.0] * self.doc_count
        if not self.average_length:
            return results
        for index, frequencies in enumerate(self.doc_frequencies):
            length = self.doc_lengths[index]
            norm = self.k1 * (1 - self.b + self.b * length / self.average_length)
            total = 0.0
            for term in query_tokens:
                frequency = frequencies.get(term)
                if not frequency:
                    continue
                total += self.idf.get(term, 0.0) * frequency * (self.k1 + 1) / (frequency + norm)
            results[index] = total
        return results


def _ranking(scores: Sequence[float], limit: int) -> list[int]:
    """점수가 0보다 큰 문서만 순위로 만든다(0점은 '안 걸림'이지 꼴찌가 아니다)."""
    ordered = sorted(
        (index for index, score in enumerate(scores) if score > 0),
        key=lambda index: (-scores[index], index),
    )
    return ordered[:limit]


def reciprocal_rank_fusion(
    rankings: Iterable[Sequence[int]], k: int = RRF_K
) -> dict[int, float]:
    """각 검색기의 순위만 사용해 병합한다. 점수 스케일을 맞출 필요가 없다."""
    fused: dict[int, float] = {}
    for ranking in rankings:
        for position, doc_index in enumerate(ranking):
            fused[doc_index] = fused.get(doc_index, 0.0) + 1.0 / (k + position + 1)
    return fused


def exact_keyword_scores(query: str, chunks: Sequence[PolicyChunk]) -> list[float]:
    """질의에 조항의 금칙어·조항번호가 **그대로** 들어 있는지 본다.

    이 도메인에서는 정확 일치가 결정적이다. "토익"은 공인어학시험 조항을 가리키는
    사실상 유일한 신호인데, BM25만 쓰면 "점수"·"성적" 같은 흔한 토큰을 공유하는 모의고사
    조항에 밀린다(실제로 그랬다). 그래서 정확 일치를 별도 랭커로 세워 RRF에 함께 넣는다.

    긴 키워드일수록 구체적이므로 길이로 가중한다 — "한국사능력검정시험"이 걸린 것은
    "대회"가 걸린 것보다 훨씬 강한 증거다.
    """
    folded_query = query.casefold()
    scores = [0.0] * len(chunks)
    for index, chunk in enumerate(chunks):
        total = 0.0
        if chunk.clause and chunk.clause.casefold() in folded_query:
            total += 5.0
        for keyword in chunk.keywords:
            token = keyword.strip().casefold()
            if len(token) < 2:
                continue
            if token in folded_query:
                total += len(token)
        scores[index] = total
    return scores


class PolicyRetriever:
    """코퍼스 하나에 대한 검색기.

    세 개의 랭커를 RRF로 병합한다.

    | 랭커 | 잡는 것 | 항상 켜짐 |
    | --- | --- | --- |
    | exact keyword | 금칙어·조항번호 정확 일치 | 예 |
    | BM25 | 어휘 중복 기반 유사도 | 예 |
    | dense | 표현이 달라도 뜻이 같은 질문 | 임베딩 제공자 설정 시 |

    `embed`가 None이면 dense 없이 동작한다. 임베딩 계산에 실패해도 예외를 올리지 않고
    나머지 랭커로 폴백한다 — 규정 검색이 안 되는 것보다 덜 정확해도 되는 것이 낫고,
    폴백 여부는 결과의 `dense_rank`가 전부 None인 것으로 확인할 수 있다.
    """

    def __init__(
        self,
        chunks: Sequence[PolicyChunk],
        embed: Callable[[list[str]], list[list[float]]] | None = None,
        chunk_vectors: Sequence[Sequence[float]] | None = None,
    ):
        self.chunks = list(chunks)
        self._embed = embed
        self._vectors = [list(vector) for vector in chunk_vectors] if chunk_vectors else None
        self._bm25 = BM25Index([tokenize(self._indexed_text(chunk)) for chunk in self.chunks])

    @staticmethod
    def _indexed_text(chunk: PolicyChunk) -> str:
        """제목·조항·키워드도 본문과 함께 색인한다.

        "3항-가"나 "TOEIC"처럼 본문에 한 번만 나오는 토큰이 실제 질의어인 경우가 많다.
        """
        parts = [chunk.title, chunk.text, chunk.clause or "", " ".join(chunk.keywords)]
        return "\n".join(part for part in parts if part)

    @property
    def is_hybrid(self) -> bool:
        return self._embed is not None and self._vectors is not None

    def search(
        self,
        query: str,
        limit: int = 5,
        *,
        policy_year: int | None = None,
        record_field: str | None = None,
    ) -> list[SearchHit]:
        candidates = [
            index
            for index, chunk in enumerate(self.chunks)
            if (policy_year is None or chunk.policy_year == policy_year)
            and chunk.applies_to(record_field)
        ]
        if not candidates:
            return []

        allowed = set(candidates)
        pool = max(limit * 4, 20)

        exact_scores = exact_keyword_scores(query, self.chunks)
        exact_ranking = [i for i in _ranking(exact_scores, pool) if i in allowed]

        sparse_scores = self._bm25.scores(tokenize(query))
        sparse_ranking = [i for i in _ranking(sparse_scores, pool) if i in allowed]

        dense_ranking = self._dense_ranking(query, allowed, pool)

        rankings = [ranking for ranking in (exact_ranking, sparse_ranking, dense_ranking) if ranking]
        if not rankings:
            return []
        fused = reciprocal_rank_fusion(rankings)

        exact_positions = {index: rank for rank, index in enumerate(exact_ranking)}
        sparse_positions = {index: rank for rank, index in enumerate(sparse_ranking)}
        dense_positions = {index: rank for rank, index in enumerate(dense_ranking)}

        ordered = sorted(fused.items(), key=lambda item: (-item[1], item[0]))
        return [
            SearchHit(
                chunk=self.chunks[index],
                score=round(score, 6),
                exact_rank=exact_positions.get(index),
                sparse_rank=sparse_positions.get(index),
                dense_rank=dense_positions.get(index),
            )
            for index, score in ordered[:limit]
        ]

    def _dense_ranking(self, query: str, allowed: set[int], pool: int) -> list[int]:
        if self._embed is None or self._vectors is None:
            return []
        try:
            query_vector = self._embed([query])[0]
        except Exception:  # 임베딩 제공자 장애가 규정 검색 전체를 막으면 안 된다
            return []
        scores = [0.0] * len(self.chunks)
        for index in allowed:
            scores[index] = _cosine(query_vector, self._vectors[index])
        return _ranking(scores, pool)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    # 음수 유사도는 순위에서 제외되도록 0으로 자른다(_ranking이 0 이하를 버린다).
    return max(0.0, dot / (left_norm * right_norm))
