import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / ".agent"


class ActivityIngestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (AGENT_DIR / "01c_activity_report_ingestion.md").read_text(encoding="utf-8")

    def test_priority_map_links_activity_to_source_report(self):
        self.assertIn("priority_map", self.text)
        self.assertIn("source_report", self.text)
        self.assertIn("REPORT_INGESTED_V1", self.text)

    def test_unmatched_candidates_are_not_silently_used(self):
        self.assertIn("매칭이 없거나 둘 이상인 항목만 사용자에게 확인한다", self.text)

    def test_output_yaml_uses_category_and_activity_key(self):
        self.assertIn('학생정보/<학생명>_<항목>.yaml', self.text)
        self.assertIn("category:", self.text)


class ActivityClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (AGENT_DIR / "01d_activity_classification.md").read_text(encoding="utf-8")

    def test_d_mode_never_writes_files(self):
        self.assertIn("파일을 저장하지 않는다", self.text)
        self.assertIn("이 모드는 어떤 파일도 쓰지 않는다", self.text)

    def test_d_mode_does_not_chain_into_02_03c_04(self):
        self.assertIn("02·03c·04 단계로 이어지지 않는다", self.text)

    def test_ambiguous_reports_are_flagged_not_guessed(self):
        self.assertIn("애매 — 교사 확인 필요", self.text)


class PhysicsIngestionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (AGENT_DIR / "01e_physics_report_ingestion.md").read_text(encoding="utf-8")

    def test_roles_are_matched_exclusively_in_fixed_order(self):
        self.assertIn("자기성찰(선택): 파일명에", self.text)
        self.assertIn("물리신문: 남은 파일 중", self.text)
        self.assertIn("주제탐구: 남은 파일 중", self.text)
        self.assertIn("role_map", self.text)

    def test_self_reflection_zero_match_is_silently_skipped(self):
        self.assertIn("자기성찰은 매칭이 0개면 조용히 생략한다", self.text)

    def test_both_required_roles_missing_stops_pipeline(self):
        self.assertIn("물리신문·주제탐구 모두 0매칭임", self.text)
        self.assertIn("REPORT_NEEDS_INPUT_V1", self.text)

    def test_output_yaml_targets_physics_suffix(self):
        self.assertIn("학생정보/<학생명>_물리세특.yaml", self.text)


class CommonEducationDraftingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (AGENT_DIR / "01f_common_education_drafting.md").read_text(encoding="utf-8")

    def test_f_mode_never_reads_docx_reports(self):
        self.assertIn("학생별 보고서(`.docx`)를 읽지 않으며", self.text)

    def test_f_mode_skips_02_03c_04(self):
        self.assertIn(
            "`02_data_structuring.md`·`03c_activity_drafting.md`·`04_evaluation.md`는 이 모드에서 사용하지 않는다",
            self.text,
        )

    def test_activity_list_is_closed_to_six_items(self):
        for item in (
            "영어듣기평가",
            "생명존중 및 자살예방교육",
            "학교폭력실태조사",
            "가정폭력예방교육",
            "아동학대예방교육",
            "다문화이해교육",
        ):
            self.assertIn(item, self.text)

    def test_target_min_bytes_is_not_applied(self):
        self.assertIn("`target_min_bytes`(권장 하한)는 이 모드에 적용하지 않는다", self.text)


class ActivityDraftingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (AGENT_DIR / "03c_activity_drafting.md").read_text(encoding="utf-8")

    def test_activities_must_not_be_merged_into_one_narrative(self):
        self.assertIn("활동 병합", self.text)

    def test_output_contract_is_draft_v1(self):
        self.assertIn("contract: DRAFT_V1", self.text)

    def test_hitl_checkpoint_is_referenced(self):
        self.assertIn("Human-in-the-Loop 초안 검토 체크포인트", self.text)


class PhysicsDraftingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (AGENT_DIR / "03e_physics_drafting.md").read_text(encoding="utf-8")

    def test_activity_order_is_role_based_not_priority(self):
        self.assertIn("자기성찰(있으면) → 물리신문 → 주제탐구", self.text)

    def test_self_reflection_paragraph_is_capped_short(self):
        self.assertIn("10~15%", self.text)

    def test_output_contract_is_draft_v1(self):
        self.assertIn("contract: DRAFT_V1", self.text)

    def test_hitl_checkpoint_is_referenced(self):
        self.assertIn("Human-in-the-Loop 초안 검토 체크포인트", self.text)


if __name__ == "__main__":
    unittest.main()
