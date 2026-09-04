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

    def test_utf8_is_required_before_first_text_read(self):
        self.assertIn("UTF-8 파일 입출력", self.master)
        self.assertIn("-Encoding UTF8", self.master)
        self.assertIn("깨지면 재시도", self.master)
        self.assertIn("NEIS 기준 바이트로 보고하지 않는다", self.master)

    def test_below_target_length_triggers_evidence_based_revision(self):
        self.assertIn("BELOW_TARGET_LENGTH", self.master)
        self.assertIn("미사용 근거", self.master)
        self.assertIn("length_exception_reason", self.master)

    def test_hitl_checkpoint_section_sits_between_step_sequence_and_linter_sections(self):
        section_heading_index = self.master.index("## 2-A. Human-in-the-Loop 체크포인트")
        step_sequence_index = self.master.index("## 2. 지침을 순서대로 실행")
        linter_section_index = self.master.index("## 3. Linter 실행 파일 결정")
        self.assertLess(step_sequence_index, section_heading_index)
        self.assertLess(section_heading_index, linter_section_index)

    def test_hitl_checkpoint_scoped_to_abce_modes_only(self):
        self.assertIn(
            "D모드와 F모드는 이 체크포인트를 사용하지 않는다", self.master
        )

    def test_hitl_uses_run_state_cli_not_manual_json_edits(self):
        self.assertIn("scripts/run_state.py", self.master)
        self.assertIn("scripts/review_cli.py", self.master)
        self.assertIn("'set-awaiting-review'", self.master)
        self.assertIn("REVIEW_REJECTED", self.master)

    def test_evaluation_waits_for_reviewed_status(self):
        self.assertIn("`status`가 `REVIEWED`이면", self.master)
        self.assertIn("`status`가 여전히 `AWAITING_REVIEW`이면", self.master)

    def test_run_state_is_the_documented_exception_to_no_intermediate_files(self):
        self.assertIn(
            "유일한 예외는 A·B·C·E모드의 초안 검토 체크포인트", self.master
        )

    def test_completion_report_cleans_up_run_state(self):
        self.assertIn("완료 보고 전에 `run_state/<식별자>.json`을 삭제", self.master)


if __name__ == "__main__":
    unittest.main()
