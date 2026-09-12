from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from version_C import llm, pipeline
from version_C.llm import ContractParseError, contract_as_text, parse_contract

VALID_DRAFT_TEXT = "탐구 활동을 통해 문제를 해결하는 과정에서 분석 능력이 드러남."
TAB_BROKEN_TEXT = "탭\t문자가 섞인 본문."
FIXED_TEXT = "탭 문자를 제거한 본문."

SOURCE_PATH = "학생정보/테스트.yaml"
OUTPUT_PATH = "세특/테스트.md"

RULES_JSON = {
    "schema_version": 1,
    "max_bytes": 1500,
    "target_min_bytes": 0,
    "byte_count": {"ascii": 1, "non_ascii": 3, "line_break": 2},
    "allowed_punctuation": ".,·'\"-()[]/%+=:;?!&",
    "allowed_symbols": "℃°±×÷",
    "forbidden_terms": [],
}


def yaml_block(text: str) -> str:
    return f"```yaml\n{text}\n```"


class FakeAgent:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system_prompt: str, user_content: str, config) -> str:
        self.calls.append((system_prompt[:20], user_content))
        if not self._responses:
            raise AssertionError("예상보다 call_agent가 더 많이 호출되었습니다.")
        return self._responses.pop(0)


class TempProject:
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "학생정보").mkdir()
        (root / "세특").mkdir()
        (root / "보고서").mkdir()
        (root / "rules.json").write_text(
            json.dumps(RULES_JSON, ensure_ascii=False), encoding="utf-8"
        )
        self.root = root
        return root

    def __exit__(self, *exc):
        self._tmp.cleanup()


def patched_pipeline(tmp_root: Path | None, agent: FakeAgent) -> ExitStack:
    stack = ExitStack()
    if tmp_root is not None:
        stack.enter_context(patch.object(pipeline, "ROOT", tmp_root))
    # 계약 요청은 llm.request_contract를 거쳐 llm.call_agent를 부른다.
    stack.enter_context(patch.object(llm, "call_agent", agent))
    stack.enter_context(patch.object(pipeline, "load_config", lambda: None))
    return stack


def structured_facts_response() -> str:
    return yaml_block(
        "contract: STRUCTURED_FACTS_V1\n"
        f'source_path: "{SOURCE_PATH}"\n'
        f'output_path: "{OUTPUT_PATH}"\n'
        "activities:\n"
        "  - activity_id: A1\n"
        '    topic: "탐구"\n'
        "    propositions:\n"
        "      - id: A1-P1\n"
        "        role: action\n"
        '        proposition: "자료를 비교함"\n'
        '        evidence_paths: ["activities[0].process[0]"]\n'
        "    supported_competencies:\n"
        '      - competency: "분석력"\n'
        '        support_ids: ["A1-P1"]\n'
    )


def draft_response(text: str = VALID_DRAFT_TEXT) -> str:
    return yaml_block(
        "contract: DRAFT_V2\n"
        f'source_path: "{SOURCE_PATH}"\n'
        f'output_path: "{OUTPUT_PATH}"\n'
        "sentences:\n"
        f'  - text: "{text}"\n'
        '    source_ids: ["A1-P1"]\n'
        f'text: "{text}"\n'
    )


def evaluate_response(final_text: str = VALID_DRAFT_TEXT) -> str:
    return yaml_block(
        "contract: EVALUATED_RESULT_V1\n"
        f'source_path: "{SOURCE_PATH}"\n'
        f'output_path: "{OUTPUT_PATH}"\n'
        "saved: true\n"
        'used_proposition_ids: ["A1-P1"]\n'
        f'final_text: "{final_text}"\n'
    )


def fix_response(final_text: str = FIXED_TEXT) -> str:
    return yaml_block(f'contract: EVALUATED_RESULT_V1\nfinal_text: "{final_text}"\n')


def start_state() -> dict:
    return {"student_key": "테스트", "mode": "yaml", "yaml_path": SOURCE_PATH}


class ContractParsingTests(unittest.TestCase):
    def test_parse_contract_extracts_yaml_block(self):
        raw = "설명 텍스트\n" + yaml_block('contract: DRAFT_V1\ntext: "본문"\n') + "\n끝"
        contract = parse_contract(raw)
        self.assertEqual(contract["contract"], "DRAFT_V1")
        self.assertEqual(contract["text"], "본문")

    def test_parse_contract_rejects_missing_block(self):
        with self.assertRaises(ContractParseError):
            parse_contract("그냥 평문 응답")

    def test_parse_contract_rejects_block_without_contract_field(self):
        with self.assertRaises(ContractParseError):
            parse_contract(yaml_block("foo: bar"))

    def test_contract_as_text_roundtrips_through_parse(self):
        original = {"contract": "STRUCTURED_FACTS_V1", "activities": []}
        text = contract_as_text(original)
        reparsed = parse_contract(yaml_block(text))
        self.assertEqual(reparsed, original)


class HappyPathTests(unittest.TestCase):
    def test_yaml_input_approved_first_try_saves_file(self):
        with TempProject() as tmp_root:
            (tmp_root / "학생정보" / "테스트.yaml").write_text(
                "schema_version: 1\n", encoding="utf-8"
            )
            agent = FakeAgent([structured_facts_response(), draft_response(), evaluate_response()])

            with patched_pipeline(tmp_root, agent):
                graph = pipeline.build_graph(MemorySaver())
                config = {"configurable": {"thread_id": "t-happy"}}
                state = graph.invoke(start_state(), config=config)
                self.assertIn("__interrupt__", state)
                self.assertEqual(state["__interrupt__"][0].value["draft_text"], VALID_DRAFT_TEXT)

                state = graph.invoke(
                    Command(resume={"approved": True, "edited_text": ""}), config=config
                )

            self.assertTrue(state.get("lint_passed"))
            saved = (tmp_root / "세특" / "테스트.md").read_text(encoding="utf-8")
            self.assertEqual(saved, VALID_DRAFT_TEXT)
            self.assertEqual(len(agent.calls), 3)

    def test_teacher_edit_at_review_overrides_model_draft(self):
        edited = "교사가 직접 고친 최종 문장."
        with TempProject() as tmp_root:
            (tmp_root / "학생정보" / "테스트.yaml").write_text(
                "schema_version: 1\n", encoding="utf-8"
            )
            agent = FakeAgent(
                [structured_facts_response(), draft_response(), evaluate_response(edited)]
            )

            with patched_pipeline(tmp_root, agent):
                graph = pipeline.build_graph(MemorySaver())
                config = {"configurable": {"thread_id": "t-edit"}}
                graph.invoke(start_state(), config=config)
                graph.invoke(
                    Command(resume={"approved": True, "edited_text": edited}), config=config
                )

            evaluate_call = agent.calls[-1]
            self.assertIn(edited, evaluate_call[1])


class RejectionLoopTests(unittest.TestCase):
    def test_reject_returns_to_draft_with_reason_then_can_be_approved(self):
        with TempProject() as tmp_root:
            (tmp_root / "학생정보" / "테스트.yaml").write_text(
                "schema_version: 1\n", encoding="utf-8"
            )
            second_draft = "다시 쓴 초안: 탐구 과정을 더 구체적으로 서술함."
            agent = FakeAgent(
                [
                    structured_facts_response(),
                    draft_response(VALID_DRAFT_TEXT),
                    draft_response(second_draft),
                    evaluate_response(second_draft),
                ]
            )

            with patched_pipeline(tmp_root, agent):
                graph = pipeline.build_graph(MemorySaver())
                config = {"configurable": {"thread_id": "t-reject"}}
                state = graph.invoke(start_state(), config=config)
                self.assertEqual(state["__interrupt__"][0].value["draft_text"], VALID_DRAFT_TEXT)

                state = graph.invoke(
                    Command(resume={"approved": False, "reason": "탐구 과정을 더 구체적으로"}),
                    config=config,
                )
                self.assertIn("__interrupt__", state)
                self.assertEqual(state["__interrupt__"][0].value["draft_text"], second_draft)

                state = graph.invoke(
                    Command(resume={"approved": True, "edited_text": ""}), config=config
                )

            self.assertTrue(state.get("lint_passed"))
            second_draft_call = agent.calls[2]
            self.assertIn("탐구 과정을 더 구체적으로", second_draft_call[1])
            saved = (tmp_root / "세특" / "테스트.md").read_text(encoding="utf-8")
            self.assertEqual(saved, second_draft)


class LintRetryTests(unittest.TestCase):
    def test_lint_failure_triggers_fix_node_then_passes(self):
        with TempProject() as tmp_root:
            (tmp_root / "학생정보" / "테스트.yaml").write_text(
                "schema_version: 1\n", encoding="utf-8"
            )
            agent = FakeAgent(
                [
                    structured_facts_response(),
                    draft_response(),
                    evaluate_response(TAB_BROKEN_TEXT),
                    fix_response(FIXED_TEXT),
                ]
            )

            with patched_pipeline(tmp_root, agent):
                graph = pipeline.build_graph(MemorySaver())
                config = {"configurable": {"thread_id": "t-fix"}}
                graph.invoke(start_state(), config=config)
                state = graph.invoke(
                    Command(resume={"approved": True, "edited_text": ""}), config=config
                )

            self.assertTrue(state.get("lint_passed"))
            self.assertEqual(state.get("lint_retry_count"), 1)
            saved = (tmp_root / "세특" / "테스트.md").read_text(encoding="utf-8")
            self.assertEqual(saved, FIXED_TEXT)

    def test_persistent_lint_failure_stops_after_max_retries(self):
        with TempProject() as tmp_root:
            (tmp_root / "학생정보" / "테스트.yaml").write_text(
                "schema_version: 1\n", encoding="utf-8"
            )
            responses = [
                structured_facts_response(),
                draft_response(),
                evaluate_response(TAB_BROKEN_TEXT),
            ]
            responses += [fix_response(TAB_BROKEN_TEXT) for _ in range(pipeline.MAX_LINT_RETRIES)]
            agent = FakeAgent(responses)

            with patched_pipeline(tmp_root, agent):
                graph = pipeline.build_graph(MemorySaver())
                config = {"configurable": {"thread_id": "t-stop"}}
                graph.invoke(start_state(), config=config)
                state = graph.invoke(
                    Command(resume={"approved": True, "edited_text": ""}), config=config
                )

            self.assertFalse(state.get("lint_passed"))
            self.assertEqual(state.get("lint_retry_count"), pipeline.MAX_LINT_RETRIES)
            self.assertFalse((tmp_root / "세특" / "테스트.md").exists())
            self.assertEqual(len(agent.calls), len(responses))


class IngestNodeTests(unittest.TestCase):
    def test_missing_yaml_file_is_reported_as_error(self):
        with TempProject() as tmp_root:
            with patch.object(pipeline, "ROOT", tmp_root):
                result = pipeline.node_ingest(
                    {"student_key": "없음", "mode": "yaml", "yaml_path": "학생정보/없음.yaml"}
                )
            self.assertIn("error", result)

    def test_structure_needs_input_short_circuits_without_drafting(self):
        needs_input = yaml_block(
            f'contract: NEEDS_INPUT_V1\nsource_path: "{SOURCE_PATH}"\nquestions: []\n'
        )
        agent = FakeAgent([needs_input])
        with patched_pipeline(None, agent):
            result = pipeline.node_structure({"yaml_content": "schema_version: 1\n"})
        # 정보 부족은 고장이 아니라 사람이 채워야 하는 공백이므로 error가 아니다.
        self.assertNotIn("error", result)
        self.assertEqual(result["needs_input"]["kind"], "questions")
        self.assertEqual(len(agent.calls), 1)


DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>탐구 보고서 본문입니다.</w:t></w:r></w:p>
  </w:body>
</w:document>
"""


def make_docx(path: Path, body: str | None = None) -> None:
    xml = DOCUMENT_XML
    if body is not None:
        xml = xml.replace("탐구 보고서 본문입니다.", body)
    with zipfile.ZipFile(path, "w") as document:
        document.writestr("word/document.xml", xml)


class PathSafetyTests(unittest.TestCase):
    def test_yaml_path_outside_student_info_dir_is_rejected(self):
        with tempfile.TemporaryDirectory() as outer:
            outer_path = Path(outer)
            tmp_root = outer_path / "project"
            tmp_root.mkdir()
            (tmp_root / "학생정보").mkdir()

            outside = outer_path / "outside.yaml"
            outside.write_text("schema_version: 1\n", encoding="utf-8")

            with patch.object(pipeline, "ROOT", tmp_root):
                result = pipeline.node_ingest(
                    {
                        "student_key": "테스트",
                        "mode": "yaml",
                        "yaml_path": "../outside.yaml",
                    }
                )
            self.assertIn("error", result)
            self.assertIn("학생정보", result["error"])

    def test_yaml_path_with_wrong_extension_is_rejected(self):
        with TempProject() as tmp_root:
            (tmp_root / "학생정보" / "테스트.txt").write_text("x", encoding="utf-8")
            with patch.object(pipeline, "ROOT", tmp_root):
                result = pipeline.node_ingest(
                    {
                        "student_key": "테스트",
                        "mode": "yaml",
                        "yaml_path": "학생정보/테스트.txt",
                    }
                )
            self.assertIn("error", result)

    def test_report_path_outside_report_dir_is_rejected(self):
        with tempfile.TemporaryDirectory() as outer:
            outer_path = Path(outer)
            tmp_root = outer_path / "project"
            tmp_root.mkdir()
            (tmp_root / "보고서").mkdir()

            outside = outer_path / "outside.docx"
            make_docx(outside)

            with patch.object(pipeline, "ROOT", tmp_root):
                result = pipeline.node_ingest(
                    {
                        "student_key": "테스트",
                        "mode": "docx",
                        "report_path": "../outside.docx",
                    }
                )
            self.assertIn("error", result)
            self.assertIn("보고서", result["error"])

    def test_student_key_with_path_separator_is_rejected(self):
        with TempProject() as tmp_root:
            with patch.object(pipeline, "ROOT", tmp_root):
                result = pipeline.node_ingest(
                    {
                        "student_key": "../evil",
                        "mode": "yaml",
                        "yaml_path": "학생정보/테스트.yaml",
                    }
                )
            self.assertIn("error", result)

    def test_model_returned_yaml_path_outside_student_info_dir_is_rejected(self):
        with tempfile.TemporaryDirectory() as outer:
            outer_path = Path(outer)
            tmp_root = outer_path / "project"
            (tmp_root / "보고서").mkdir(parents=True)
            (tmp_root / "학생정보").mkdir()

            report_path = tmp_root / "보고서" / "테스트.docx"
            make_docx(report_path)

            malicious = yaml_block(
                "contract: REPORT_INGESTED_V1\n"
                'yaml_path: "../outside.yaml"\n'
                'yaml_body: "schema_version: 1"\n'
            )
            # 계약 검증이 1회 repair를 요청하므로 같은 응답을 두 번 준비한다.
            agent = FakeAgent([malicious, malicious])

            with patched_pipeline(tmp_root, agent):
                result = pipeline.node_ingest(
                    {
                        "student_key": "테스트",
                        "mode": "docx",
                        "report_path": "보고서/테스트.docx",
                    }
                )

            # 계약 검증에서 걸리므로 needs_input으로 넘어간다. 핵심은 파일이 쓰이지 않는 것.
            self.assertIn("needs_input", result)
            self.assertIn("yaml_path", result["needs_input"]["problems"])
            self.assertFalse((outer_path / "outside.yaml").exists())


if __name__ == "__main__":
    unittest.main()
