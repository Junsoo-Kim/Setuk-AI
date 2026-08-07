import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MasterRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.master = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        cls.cline_rule = (ROOT / ".clinerules" / "00-setuk-master.md").read_text(
            encoding="utf-8"
        )
        cls.cline_workflow = (ROOT / ".clinerules" / "workflows" / "setuk.md").read_text(
            encoding="utf-8"
        )

    def test_current_tool_instruction_files_exist(self):
        self.assertTrue((ROOT / "AGENTS.md").is_file())
        self.assertTrue((ROOT / ".clinerules" / "00-setuk-master.md").is_file())
        self.assertTrue((ROOT / ".clinerules" / "workflows" / "setuk.md").is_file())

    def test_agent_stages_are_ordered(self):
        positions = [
            self.master.index(".agent/01_report_ingestion.md"),
            self.master.index(".agent/02_data_structuring.md"),
            self.master.index(".agent/03_drafting.md"),
            self.master.index(".agent/04_evaluation.md"),
        ]
        self.assertEqual(positions, sorted(positions))

    def test_missing_input_contract_stops_pipeline(self):
        self.assertIn("REPORT_NEEDS_INPUT_V1", self.master)
        self.assertIn("NEEDS_INPUT_V1", self.master)
        self.assertIn("이후 지침을 읽거나 결과 파일을 만들지 않는다", self.master)

    def test_only_portable_python_is_allowed(self):
        self.assertIn("python_portable/python.exe", self.master)
        self.assertIn("시스템 PATH의 `python`, `python3`, `py`는 사용하지 않는다", self.master)

    def test_every_linter_run_requires_approval(self):
        self.assertIn("Linter를 실행해도 될까요?", self.master)
        self.assertIn("Linter를 다시 실행해도 될까요?", self.master)
        self.assertIn("승인 없이 재검사하지 않는다", self.master)

    def test_all_linter_exit_codes_are_handled(self):
        for code in ("종료 코드 `0`", "종료 코드 `1`", "종료 코드 `2`"):
            self.assertIn(code, self.master)

    def test_loop_has_safety_stops(self):
        self.assertIn("두 번 연속 반복", self.master)
        self.assertIn("재검사를 5회", self.master)

    def test_cline_files_delegate_to_common_master(self):
        self.assertIn("`AGENTS.md`", self.cline_rule)
        self.assertIn("`AGENTS.md`", self.cline_workflow)
        self.assertIn("매 Linter 실행 전", self.cline_rule)

    def test_report_and_yaml_entry_modes_are_distinct(self):
        self.assertIn("보고서 입력 모드", self.master)
        self.assertIn("기존 YAML 직접 입력 모드", self.master)
        self.assertIn("경로를 명시한 경우에만", self.master)


if __name__ == "__main__":
    unittest.main()
