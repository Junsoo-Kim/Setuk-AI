"""계약 스키마 검증과 repair 재시도 정책 테스트.

여기서 막고 싶은 실제 실패는 다음 네 가지다.

1. 모델이 설명 문장과 함께 불완전한 YAML을 돌려준다.
2. 모델이 다른 단계의 계약을 돌려준다.
3. 모델이 사실 장부에 없는 명제 ID를 인용한다(근거 추적이 끊긴다).
4. 모델이 다른 학생의 경로를 섞어 쓴다(학생 간 데이터 혼선).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from version_C import contracts, llm
from version_C.contracts import ContractValidationError

SOURCE = "학생정보/테스트.yaml"
OUTPUT = "세특/테스트.md"


def facts(**overrides) -> dict:
    data = {
        "contract": "STRUCTURED_FACTS_V1",
        "source_path": SOURCE,
        "output_path": OUTPUT,
        "activities": [
            {
                "activity_id": "A1",
                "topic": "탐구",
                "propositions": [
                    {"id": "A1-P1", "role": "action", "proposition": "자료를 비교함"},
                    {"id": "A1-P2", "role": "result", "proposition": "결론을 도출함"},
                ],
                "supported_competencies": [
                    {"competency": "분석력", "support_ids": ["A1-P1"]}
                ],
            }
        ],
    }
    data.update(overrides)
    return data


def draft(**overrides) -> dict:
    data = {
        "contract": "DRAFT_V1",
        "source_path": SOURCE,
        "output_path": OUTPUT,
        "used_proposition_ids": ["A1-P1"],
        "text": "탐구 과정에서 자료를 비교하고 결론을 도출함.",
    }
    data.update(overrides)
    return data


def draft_v2(**overrides) -> dict:
    data = {
        "contract": "DRAFT_V2",
        "source_path": SOURCE,
        "output_path": OUTPUT,
        "sentences": [{"text": "탐구 과정에서 자료를 비교함.", "source_ids": ["A1-P1"]}],
        "text": "탐구 과정에서 자료를 비교하고 결론을 도출함.",
    }
    data.update(overrides)
    return data


class SchemaTests(unittest.TestCase):
    def test_valid_structured_facts_parses(self):
        parsed = contracts.parse_stage_contract(facts(), "structure")
        self.assertIsInstance(parsed, contracts.StructuredFactsV1)
        self.assertEqual(parsed.proposition_ids(), {"A1-P1", "A1-P2"})

    def test_missing_required_field_is_rejected(self):
        broken = draft()
        del broken["text"]
        with self.assertRaises(ContractValidationError) as ctx:
            contracts.parse_stage_contract(broken, "draft")
        self.assertIn("text", str(ctx.exception))

    def test_empty_text_is_rejected(self):
        with self.assertRaises(ContractValidationError):
            contracts.parse_stage_contract(draft(text=""), "draft")

    def test_wrong_stage_contract_is_rejected(self):
        with self.assertRaises(ContractValidationError) as ctx:
            contracts.parse_stage_contract(facts(), "draft")
        self.assertIn("DRAFT_V1", str(ctx.exception))

    def test_unknown_contract_name_is_rejected(self):
        with self.assertRaises(ContractValidationError):
            contracts.parse_stage_contract({"contract": "SOMETHING_ELSE_V9"}, "draft")

    def test_needs_input_is_accepted_at_structure_stage(self):
        parsed = contracts.parse_stage_contract(
            {"contract": "NEEDS_INPUT_V1", "source_path": SOURCE, "questions": []}, "structure"
        )
        self.assertIsInstance(parsed, contracts.NeedsInputV1)

    def test_unknown_field_is_rejected(self):
        """지침에 없는 필드를 모델이 지어내면 거부한다.

        느슨하게 두면 `confidence: 0.9`처럼 아무도 검증하지 않는 값이 통과하고,
        하류에서 근거인 척 쓰일 수 있다. 계약의 요점은 무엇이 올지 미리 정하는 것이다.
        """
        with self.assertRaises(ContractValidationError) as ctx:
            contracts.parse_stage_contract(draft(confidence=0.9), "draft")
        self.assertIn("confidence", str(ctx.exception))

    def test_fields_defined_by_the_agent_instructions_are_accepted(self):
        """`.agent/*.md`가 정의한 필드는 전부 통과해야 한다.

        스키마를 잠근 대가로, 지침에 있는 필드를 빠뜨리면 파이프라인이 즉시 멈춘다.
        이 테스트가 그 회귀를 잡는다.
        """
        full_draft = draft(
            omitted_proposition_ids=[],
            target_byte_range=[1400, 1500],
            estimated_byte_count=1450,
            length_exception_reason=None,
            teacher_evaluation={
                "competency": "분석력",
                "support_ids": ["A1-P1"],
                "expression": "근거와 연결된 평가",
            },
        )
        parsed = contracts.parse_stage_contract(full_draft, "draft")
        self.assertEqual(parsed.target_byte_range, [1400, 1500])

        full_draft_v2 = draft_v2(
            sentences=[
                {"text": "탐구 과정에서 자료를 비교함.", "source_ids": ["A1-P1"], "confidence": 0.9}
            ],
            omitted_proposition_ids=[],
            target_byte_range=[1400, 1500],
            estimated_byte_count=1450,
            length_exception_reason=None,
            teacher_evaluation={
                "competency": "분석력",
                "support_ids": ["A1-P1"],
                "expression": "근거와 연결된 평가",
            },
        )
        parsed_v2 = contracts.parse_stage_contract(full_draft_v2, "draft")
        self.assertEqual(parsed_v2.sentences[0].confidence, 0.9)

        full_result = {
            "contract": "EVALUATED_RESULT_V1",
            "source_path": SOURCE,
            "output_path": OUTPUT,
            "saved": True,
            "used_proposition_ids": ["A1-P1"],
            "byte_count": 1450,
            "target_byte_range": [1400, 1500],
            "length_exception_reason": None,
            "review": {
                "factual_fidelity": "satisfied",
                "specificity": "satisfied",
                "coherence": "satisfied",
                "competency_evidence": "satisfied",
                "growth": "satisfied",
                "information_efficiency": "satisfied",
                "teacher_evaluation": "satisfied",
            },
            "final_text": "본문",
        }
        contracts.parse_stage_contract(full_result, "evaluate")

        full_facts = facts(
            subject={"name": "물리학I", "semester": "1학기"},
            constraints={"target_bytes": 1500, "emphasis": [], "exclude": []},
            overall_observation={
                "proposition": "종합 관찰",
                "evidence_paths": ["overall_observation"],
            },
            unused_fields=[],
        )
        contracts.parse_stage_contract(full_facts, "structure")

        full_ingested = {
            "contract": "REPORT_INGESTED_V1",
            "report_path": "보고서/테스트.docx",
            "student_name": "학생",
            "yaml_path": SOURCE,
            "yaml_body": "schema_version: 1",
            "saved": True,
            "activity_count": 1,
            "uncertain_fields": [],
            "warnings": [],
            "excluded_uncertain_facts": [],
        }
        contracts.parse_stage_contract(full_ingested, "ingest")


class PathSafetyTests(unittest.TestCase):
    def test_parent_traversal_in_output_path_is_rejected(self):
        with self.assertRaises(ContractValidationError) as ctx:
            contracts.parse_stage_contract(draft(output_path="세특/../../etc/passwd.md"), "draft")
        self.assertIn("output_path", str(ctx.exception))

    def test_absolute_path_is_rejected(self):
        with self.assertRaises(ContractValidationError):
            contracts.parse_stage_contract(draft(output_path="C:/temp/evil.md"), "draft")

    def test_wrong_directory_is_rejected(self):
        with self.assertRaises(ContractValidationError):
            contracts.parse_stage_contract(draft(output_path="보고서/테스트.md"), "draft")

    def test_wrong_extension_is_rejected(self):
        with self.assertRaises(ContractValidationError):
            contracts.parse_stage_contract(draft(output_path="세특/테스트.txt"), "draft")

    def test_backslash_path_is_normalized(self):
        parsed = contracts.parse_stage_contract(draft(output_path="세특\\테스트.md"), "draft")
        self.assertEqual(parsed.output_path, "세특/테스트.md")


class ReferentialIntegrityTests(unittest.TestCase):
    def test_duplicate_proposition_ids_are_rejected(self):
        broken = facts()
        broken["activities"][0]["propositions"][1]["id"] = "A1-P1"
        with self.assertRaises(ContractValidationError) as ctx:
            contracts.parse_stage_contract(broken, "structure")
        self.assertIn("중복", str(ctx.exception))

    def test_malformed_proposition_id_is_rejected(self):
        broken = facts()
        broken["activities"][0]["propositions"][0]["id"] = "첫번째명제"
        with self.assertRaises(ContractValidationError):
            contracts.parse_stage_contract(broken, "structure")

    def test_competency_support_id_must_exist(self):
        broken = facts()
        broken["activities"][0]["supported_competencies"][0]["support_ids"] = ["A9-P9"]
        with self.assertRaises(ContractValidationError):
            contracts.parse_stage_contract(broken, "structure")

    def test_draft_citing_unknown_proposition_is_rejected(self):
        parsed_facts = contracts.parse_stage_contract(facts(), "structure")
        parsed_draft = contracts.parse_stage_contract(
            draft(used_proposition_ids=["A1-P1", "A7-P3"]), "draft"
        )
        with self.assertRaises(ContractValidationError) as ctx:
            contracts.validate_draft_against_facts(parsed_draft, parsed_facts)
        self.assertIn("A7-P3", str(ctx.exception))

    def test_draft_with_mismatched_source_path_is_rejected(self):
        parsed_facts = contracts.parse_stage_contract(facts(), "structure")
        parsed_draft = contracts.parse_stage_contract(
            draft(source_path="학생정보/다른학생.yaml"), "draft"
        )
        with self.assertRaises(ContractValidationError) as ctx:
            contracts.validate_draft_against_facts(parsed_draft, parsed_facts)
        self.assertIn("source_path", str(ctx.exception))

    def test_draft_matching_facts_passes(self):
        parsed_facts = contracts.parse_stage_contract(facts(), "structure")
        parsed_draft = contracts.parse_stage_contract(draft(), "draft")
        contracts.validate_draft_against_facts(parsed_draft, parsed_facts)  # 예외 없음

    def test_draft_v1_is_migrated_to_v2_on_parse(self):
        parsed_draft = contracts.parse_stage_contract(draft(), "draft")
        self.assertEqual(parsed_draft.contract, "DRAFT_V2")
        self.assertEqual(len(parsed_draft.sentences), 1)
        self.assertEqual(parsed_draft.sentences[0].source_ids, ["A1-P1"])
        self.assertEqual(parsed_draft.used_proposition_ids, ["A1-P1"])

    def test_draft_v2_rejects_empty_sentences(self):
        with self.assertRaises(ContractValidationError):
            contracts.parse_stage_contract(draft_v2(sentences=[]), "draft")

    def test_draft_v2_rejects_confidence_out_of_range(self):
        with self.assertRaises(ContractValidationError):
            contracts.parse_stage_contract(
                draft_v2(sentences=[{"text": "x", "source_ids": ["A1-P1"], "confidence": 1.5}]),
                "draft",
            )

    def test_used_proposition_ids_dedupes_across_sentences(self):
        parsed_draft = contracts.parse_stage_contract(
            draft_v2(
                sentences=[
                    {"text": "첫 문장.", "source_ids": ["A1-P1"]},
                    {"text": "둘째 문장.", "source_ids": ["A1-P1", "A1-P2"]},
                ]
            ),
            "draft",
        )
        self.assertEqual(parsed_draft.used_proposition_ids, ["A1-P1", "A1-P2"])

    def test_sentence_evidence_citing_unknown_proposition_is_rejected(self):
        parsed_facts = contracts.parse_stage_contract(facts(), "structure")
        parsed_draft = contracts.parse_stage_contract(
            draft_v2(
                sentences=[
                    {"text": "자료를 비교함.", "source_ids": ["A1-P1"]},
                    {"text": "존재하지 않는 근거.", "source_ids": ["A9-P9"]},
                ]
            ),
            "draft",
        )
        with self.assertRaises(ContractValidationError) as ctx:
            contracts.validate_draft_against_facts(parsed_draft, parsed_facts)
        self.assertIn("A9-P9", str(ctx.exception))

    def test_sentence_evidence_matching_facts_passes(self):
        parsed_facts = contracts.parse_stage_contract(facts(), "structure")
        parsed_draft = contracts.parse_stage_contract(
            draft_v2(
                sentences=[
                    {"text": "자료를 비교함.", "source_ids": ["A1-P1"]},
                ]
            ),
            "draft",
        )
        contracts.validate_draft_against_facts(parsed_draft, parsed_facts)  # 예외 없음


class ByteCountTests(unittest.TestCase):
    def test_matching_byte_count_reports_nothing(self):
        self.assertIsNone(contracts.verify_byte_count(1450, 1450))

    def test_absent_byte_count_reports_nothing(self):
        self.assertIsNone(contracts.verify_byte_count(None, 1450))

    def test_mismatch_is_reported_and_code_value_wins(self):
        message = contracts.verify_byte_count(1450, 1502)
        self.assertIn("1450", message)
        self.assertIn("1502", message)


class ScriptedAgent:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def __call__(self, system_prompt, user_content, config):
        self.prompts.append(user_content)
        return self.responses.pop(0)


def block(body: str) -> str:
    return f"```yaml\n{body}\n```"


class RepairStrategyTests(unittest.TestCase):
    """형식 오류는 한 번만 고쳐 달라고 요청한다(무한 재시도로 비용을 태우지 않는다)."""

    def test_broken_first_response_is_repaired_once(self):
        agent = ScriptedAgent(
            [
                "설명을 곁들인 응답입니다.\n" + block('contract: DRAFT_V1\ntext: "본문"'),
                block(
                    "contract: DRAFT_V1\n"
                    f'source_path: "{SOURCE}"\n'
                    f'output_path: "{OUTPUT}"\n'
                    'used_proposition_ids: ["A1-P1"]\n'
                    'text: "본문"\n'
                ),
            ]
        )
        metrics = llm.CallMetrics(stage="draft")
        result = llm.request_contract("sys", "user", None, "draft", agent=agent, metrics=metrics)
        self.assertEqual(result.text, "본문")
        self.assertEqual(metrics.attempts, 2)
        self.assertEqual(metrics.repairs, 1)
        # repair 프롬프트에는 실패 사유가 그대로 들어간다.
        self.assertIn("source_path", agent.prompts[1])

    def test_repair_is_attempted_at_most_once(self):
        bad = block('contract: DRAFT_V1\ntext: "본문"')
        agent = ScriptedAgent([bad, bad])
        with self.assertRaises(ContractValidationError):
            llm.request_contract("sys", "user", None, "draft", agent=agent)
        self.assertEqual(len(agent.prompts), 2)

    def test_missing_yaml_block_also_triggers_repair(self):
        agent = ScriptedAgent(
            [
                "죄송합니다. 계약 블록 없이 평문으로 답했습니다.",
                block(
                    "contract: DRAFT_V1\n"
                    f'source_path: "{SOURCE}"\n'
                    f'output_path: "{OUTPUT}"\n'
                    'text: "본문"\n'
                ),
            ]
        )
        result = llm.request_contract("sys", "user", None, "draft", agent=agent)
        self.assertEqual(result.text, "본문")

    def test_cross_stage_validation_failure_also_triggers_repair(self):
        parsed_facts = contracts.parse_stage_contract(facts(), "structure")

        def check(contract):
            contracts.validate_draft_against_facts(contract, parsed_facts)

        good = block(
            "contract: DRAFT_V1\n"
            f'source_path: "{SOURCE}"\n'
            f'output_path: "{OUTPUT}"\n'
            'used_proposition_ids: ["A1-P1"]\n'
            'text: "본문"\n'
        )
        bad = block(
            "contract: DRAFT_V1\n"
            f'source_path: "{SOURCE}"\n'
            f'output_path: "{OUTPUT}"\n'
            'used_proposition_ids: ["A9-P9"]\n'
            'text: "본문"\n'
        )
        agent = ScriptedAgent([bad, good])
        metrics = llm.CallMetrics(stage="draft")
        result = llm.request_contract(
            "sys", "user", None, "draft", agent=agent, extra_validator=check, metrics=metrics
        )
        self.assertEqual(result.used_proposition_ids, ["A1-P1"])
        self.assertEqual(metrics.repairs, 1)

    def test_metrics_record_failures_without_raw_response(self):
        bad = block('contract: DRAFT_V1\ntext: "본문"')
        agent = ScriptedAgent([bad, bad])
        metrics = llm.CallMetrics(stage="draft")
        with self.assertRaises(ContractValidationError):
            llm.request_contract("sys", "user", None, "draft", agent=agent, metrics=metrics)
        detail = metrics.as_detail()
        self.assertEqual(len(detail["parse_failures"]), 2)
        self.assertEqual(len(detail["response_hashes"]), 2)
        # 응답 전문이 아니라 해시만 남는다.
        for value in detail["response_hashes"]:
            self.assertEqual(len(value), 16)
            self.assertNotIn("본문", value)


if __name__ == "__main__":
    unittest.main()
