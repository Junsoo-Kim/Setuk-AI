import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / ".agent"


class AgentInstructionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ingestion = (AGENT_DIR / "01_report_ingestion.md").read_text(encoding="utf-8")
        cls.structuring = (AGENT_DIR / "02_data_structuring.md").read_text(encoding="utf-8")
        cls.drafting = (AGENT_DIR / "03_drafting.md").read_text(encoding="utf-8")
        cls.evaluation = (AGENT_DIR / "04_evaluation.md").read_text(encoding="utf-8")

    def test_all_pipeline_files_exist(self):
        expected = {
            "README.md",
            "01_report_ingestion.md",
            "02_data_structuring.md",
            "03_drafting.md",
            "04_evaluation.md",
        }
        self.assertEqual(expected, {path.name for path in AGENT_DIR.glob("*.md")})

    def test_contracts_connect_in_order(self):
        self.assertIn("REPORT_INGESTED_V1", self.ingestion)
        self.assertIn("학생정보/<학생명>.yaml", self.ingestion)
        self.assertIn("STRUCTURED_FACTS_V1", self.structuring)
        self.assertIn("STRUCTURED_FACTS_V1", self.drafting)
        self.assertIn("DRAFT_V1", self.drafting)
        self.assertIn("STRUCTURED_FACTS_V1", self.evaluation)
        self.assertIn("DRAFT_V1", self.evaluation)
        self.assertIn("EVALUATED_RESULT_V1", self.evaluation)

    def test_missing_input_stops_pipeline(self):
        self.assertIn("REPORT_NEEDS_INPUT_V1", self.ingestion)
        self.assertIn("NEEDS_INPUT_V1", self.structuring)
        self.assertIn("NEEDS_INPUT_V1", self.drafting)
        self.assertIn("이후 단계를 실행하지 않고", (AGENT_DIR / "README.md").read_text(encoding="utf-8"))

    def test_each_stage_forbids_unsupported_facts(self):
        self.assertIn("추가하지 않는다", self.ingestion)
        self.assertIn("추가하지 않는다", self.structuring)
        self.assertIn("추측하지 않는다", self.drafting)
        self.assertIn("추정하지 않는다", self.evaluation)

    def test_final_output_uses_source_identifier(self):
        self.assertIn('output_path: "세특/<식별자>.md"', self.structuring)
        self.assertIn("계약의 `output_path`를 그대로 사용", self.evaluation)

    def test_linter_is_deferred_to_master_rules(self):
        self.assertIn("이 단계에서는 Linter를 실행하지 않는다", self.evaluation)

    def test_report_ingestion_uses_portable_utf8_extractor(self):
        self.assertIn("python_portable\\python.exe", self.ingestion)
        self.assertIn("scripts\\extract_docx.py", self.ingestion)
        self.assertIn("UTF-8 JSON", self.ingestion)
        self.assertIn("다른 학생 보고서를 함께 열지 않는다", self.ingestion)

    def test_report_ingestion_preserves_evidence_boundaries(self):
        self.assertIn("조사로 얻은 설명", self.ingestion)
        self.assertIn("실패하거나 결과를 얻지 못한 실험", self.ingestion)
        self.assertIn("교사의 직접 관찰로 바꾸지 않는다", self.ingestion)

    def test_localized_report_typos_warn_without_blocking(self):
        self.assertIn("비차단 불일치", self.ingestion)
        self.assertIn("자동으로 고쳐 쓰지 않는다", self.ingestion)
        self.assertIn("REPORT_INGESTED_V1.warnings", self.ingestion)
        self.assertIn("excluded_uncertain_facts", self.ingestion)
        self.assertIn("해당 무늬 길이는 제외", self.ingestion)

    def test_blocking_report_conflicts_are_limited_to_core_facts(self):
        self.assertIn("차단 불일치", self.ingestion)
        self.assertIn("학생 신원을 확정할 수 없음", self.ingestion)
        self.assertIn("불확실한 부분만 제외하고 진행할 수 없음", self.ingestion)

    def test_missing_report_response_identifies_active_workspace(self):
        self.assertIn("active_report_directory", self.ingestion)
        self.assertIn("available_docx_filenames", self.ingestion)
        self.assertIn("새 버전 ZIP", (ROOT / "보고서" / "README.md").read_text(encoding="utf-8"))

    def test_teacher_evaluation_is_required_and_evidence_based(self):
        self.assertIn("평가 문장 또는 절을 최소 1개", self.drafting)
        self.assertIn("teacher_evaluation", self.drafting)
        self.assertIn("support_ids", self.drafting)
        self.assertIn("교사 평가성", self.evaluation)
        self.assertIn("supported_competencies", self.evaluation)
        self.assertIn("근거 부족으로 저장을 중단", self.evaluation)

    def test_activity_listing_alone_is_rejected(self):
        self.assertIn("활동 내용의 나열로 끝나지 않도록", self.drafting)
        self.assertIn("나열형 문장 판정과 보완", self.evaluation)
        self.assertIn("평가 문장 또는 절이 최소 1개 있음", self.evaluation)


if __name__ == "__main__":
    unittest.main()
