import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / ".agent"


class AgentInstructionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.structuring = (AGENT_DIR / "01_data_structuring.md").read_text(encoding="utf-8")
        cls.drafting = (AGENT_DIR / "02_drafting.md").read_text(encoding="utf-8")
        cls.evaluation = (AGENT_DIR / "03_evaluation.md").read_text(encoding="utf-8")

    def test_all_pipeline_files_exist(self):
        expected = {
            "README.md",
            "01_data_structuring.md",
            "02_drafting.md",
            "03_evaluation.md",
        }
        self.assertEqual(expected, {path.name for path in AGENT_DIR.glob("*.md")})

    def test_contracts_connect_in_order(self):
        self.assertIn("STRUCTURED_FACTS_V1", self.structuring)
        self.assertIn("STRUCTURED_FACTS_V1", self.drafting)
        self.assertIn("DRAFT_V1", self.drafting)
        self.assertIn("STRUCTURED_FACTS_V1", self.evaluation)
        self.assertIn("DRAFT_V1", self.evaluation)
        self.assertIn("EVALUATED_RESULT_V1", self.evaluation)

    def test_missing_input_stops_pipeline(self):
        self.assertIn("NEEDS_INPUT_V1", self.structuring)
        self.assertIn("NEEDS_INPUT_V1", self.drafting)
        self.assertIn("이후 단계를 실행하지 않고", (AGENT_DIR / "README.md").read_text(encoding="utf-8"))

    def test_each_stage_forbids_unsupported_facts(self):
        self.assertIn("추가하지 않는다", self.structuring)
        self.assertIn("추측하지 않는다", self.drafting)
        self.assertIn("추정하지 않는다", self.evaluation)

    def test_final_output_uses_source_identifier(self):
        self.assertIn('output_path: "세특/<식별자>.md"', self.structuring)
        self.assertIn("계약의 `output_path`를 그대로 사용", self.evaluation)

    def test_linter_is_deferred_to_master_rules(self):
        self.assertIn("이 단계에서는 Linter를 실행하지 않는다", self.evaluation)


if __name__ == "__main__":
    unittest.main()
