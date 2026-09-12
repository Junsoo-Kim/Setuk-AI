"""`.agent/*.md`가 선언하는 계약 필드와 `contracts.py`의 Pydantic 필드 일치 검증.

`test_contracts.py`는 이미 "모델이 필드를 빠뜨리면 코드가 거부하는가"를 검증한다.
이 파일은 반대 방향을 본다 — 지침 문서(`.agent/*.md`)에 적힌 계약 예시 필드와
`contracts.py`의 Pydantic 모델 필드가 서로 어긋나지 않았는지다.

두 방향의 실수는 원인이 다르다.
- 코드만 고치고 지침을 못 고치면: 모델이 지침대로 응답해도 코드가 알 수 없는
  필드라며 거부하거나(너무 엄격), 지침에 없는 필드를 모델이 채우도록 코드가
  기대하게 된다(모델이 절대 채울 수 없음).
- 지침만 고치고 코드를 못 고치면: 지침에 새 필드를 추가해도 `extra="forbid"`
  설정 때문에 모델이 그 필드를 채워 보내는 순간 파싱이 거부된다.

이 테스트는 `.agent/*.md`의 예시 yaml 블록을 그대로 파싱해서 최상위 필드
집합을 뽑고, 대응하는 Pydantic 모델의 필드 집합과 비교한다. 코드가 저장을
대행하기 위해 지침에는 없지만 추가로 요구하는 필드(`yaml_body`, `final_text`)는
`contracts.py`의 클래스 docstring에 그 사유가 적혀 있으므로 허용 목록으로 뺀다.

중첩 구조(예: `activities[].propositions[]`)는 다루지 않는다 — 최상위 계약
필드가 어긋나는 사고가 가장 흔하고 비용이 크며(단계 전체가 막힌다), 중첩까지
다루면 이 테스트 자체가 지침 문서 서식 변화에 너무 민감해진다.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from version_C import prompts
from version_C.contracts import (
    DraftV1,
    DraftV2,
    EvaluatedResultV1,
    NeedsInputV1,
    ReportIngestedV1,
    ReportNeedsInputV1,
    StructuredFactsV1,
)

_YAML_BLOCK_RE = re.compile(r"```ya?ml\s*\n(.*?)```", re.DOTALL)

# 지침 문서에 여전히 나오는(활성) 계약뿐 아니라, 과거 데이터를 읽기 위해 코드에만
# 남아 있는 계약(DRAFT_V1 — DRAFT_V2로 이전하는 마이그레이션 소스)도 여기 둔다.
# "선언된 계약 이름이 모델과 대응하는가"는 이 전체 사전으로 확인하고, "지침 문서가
# 빠짐없이 다 검사됐는가"는 아래 ACTIVE_CONTRACTS로 따로 확인한다.
NAME_TO_MODEL = {
    "REPORT_NEEDS_INPUT_V1": ReportNeedsInputV1,
    "REPORT_INGESTED_V1": ReportIngestedV1,
    "NEEDS_INPUT_V1": NeedsInputV1,
    "STRUCTURED_FACTS_V1": StructuredFactsV1,
    "DRAFT_V1": DraftV1,  # 지침엔 더 이상 없음: DRAFT_V2로의 마이그레이션 소스로만 남아 있다.
    "DRAFT_V2": DraftV2,
    "EVALUATED_RESULT_V1": EvaluatedResultV1,
}

# 현재 지침 문서(.agent/*.md)가 실제로 선언해야 하는 계약. DRAFT_V1처럼 코드에만
# 남은 레거시 계약은 여기 넣지 않는다 — 넣으면 "지침에 없는데 왜 안 나왔냐"고
# 테스트가 스스로 잘못 실패한다.
ACTIVE_CONTRACTS = {
    "REPORT_NEEDS_INPUT_V1",
    "REPORT_INGESTED_V1",
    "NEEDS_INPUT_V1",
    "STRUCTURED_FACTS_V1",
    "DRAFT_V2",
    "EVALUATED_RESULT_V1",
}

# 지침 문서에는 없지만 코드가 파일 저장을 대행하려고 추가로 요구하는 필드.
# 이유는 contracts.py의 각 클래스 docstring에 적혀 있다.
CODE_ONLY_EXTRA_FIELDS: dict[str, set[str]] = {
    "REPORT_INGESTED_V1": {"yaml_body"},
    "EVALUATED_RESULT_V1": {"final_text"},
}

# 이 지침 파일이 어떤 계약을 선언하는지 (다른 계약이 새 지침 파일에 추가되면 여기도 늘린다).
AGENT_FILES = (
    "01_report_ingestion.md",
    "02_data_structuring.md",
    "03_drafting.md",
    "04_evaluation.md",
)


def _extract_contract_blocks(markdown: str) -> list[dict]:
    """지침 문서의 yaml 코드 블록 중 `contract:` 필드가 있는 것만 dict로 뽑는다.

    입력 예시(스키마 템플릿 등)처럼 `contract` 필드가 없는 블록은 계약 선언이
    아니므로 건너뛴다.
    """
    blocks = []
    for match in _YAML_BLOCK_RE.finditer(markdown):
        try:
            data = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and isinstance(data.get("contract"), str):
            blocks.append(data)
    return blocks


class ContractFieldSyncTests(unittest.TestCase):
    def _iter_declared_contracts(self):
        for filename in AGENT_FILES:
            markdown = (prompts.AGENT_DIR / filename).read_text(encoding="utf-8")
            for block in _extract_contract_blocks(markdown):
                yield filename, block

    def test_every_declared_contract_name_is_a_known_model(self):
        for filename, block in self._iter_declared_contracts():
            name = block["contract"]
            self.assertIn(
                name,
                NAME_TO_MODEL,
                f"{filename}이 선언한 계약 {name!r}에 대응하는 Pydantic 모델이 없습니다.",
            )

    def test_declared_fields_match_model_fields(self):
        checked_contracts: set[str] = set()
        for filename, block in self._iter_declared_contracts():
            name = block["contract"]
            model = NAME_TO_MODEL.get(name)
            if model is None:
                continue  # 위 테스트가 이미 별도로 잡는다.
            checked_contracts.add(name)

            doc_fields = set(block.keys()) - {"contract"}
            model_fields = set(model.model_fields.keys()) - {"contract"}
            model_fields -= CODE_ONLY_EXTRA_FIELDS.get(name, set())

            self.assertEqual(
                model_fields,
                doc_fields,
                f"{filename}의 {name} 예시 필드와 contracts.py의 {model.__name__} "
                f"필드가 어긋납니다. 지침 전용: {doc_fields - model_fields}, "
                f"코드 전용: {model_fields - doc_fields}",
            )

        # 지침 문서 구성이 바뀌어 블록을 하나도 못 찾는 경우, 테스트가 조용히
        # 통과하며 아무것도 검증하지 않게 되는 것을 막는다.
        self.assertEqual(checked_contracts, ACTIVE_CONTRACTS)


if __name__ == "__main__":
    unittest.main()
