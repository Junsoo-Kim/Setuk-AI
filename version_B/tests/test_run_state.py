import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import review_cli
import run_state as rs


class RunStateSchemaTests(unittest.TestCase):
    def test_new_state_has_required_shape(self):
        state = rs.new_state("김준수", "A")
        rs.validate_state(state)
        self.assertEqual("INITIALIZED", state["status"])
        self.assertEqual(
            {
                "report_ingestion",
                "data_structuring",
                "drafting",
                "review",
                "evaluation",
                "lint",
            },
            set(state["steps"]),
        )

    def test_new_state_rejects_disallowed_mode(self):
        with self.assertRaises(rs.RunStateError):
            rs.new_state("김준수", "D")

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "김준수.json"
            state = rs.new_state("김준수", "B")
            rs.save_state(path, state)
            loaded = rs.load_state(path)
            self.assertEqual("김준수", loaded["student_key"])
            self.assertEqual("INITIALIZED", loaded["status"])
            self.assertIn("updated_at", loaded)

    def test_validate_rejects_unknown_status(self):
        state = rs.new_state("김준수", "A")
        state["status"] = "MADE_UP_STATUS"
        with self.assertRaises(rs.RunStateError):
            rs.validate_state(state)

    def test_validate_rejects_wrong_schema_version(self):
        state = rs.new_state("김준수", "A")
        state["schema_version"] = 99
        with self.assertRaises(rs.RunStateError):
            rs.validate_state(state)


class TransitionGuardTests(unittest.TestCase):
    def test_set_drafted_requires_structured_or_rejected(self):
        state = rs.new_state("김준수", "A")
        with self.assertRaises(rs.RunStateError):
            rs.set_drafted(state, "초안 본문")

    def test_full_happy_path_transitions(self):
        state = rs.new_state("김준수", "A")
        state = rs.set_report_ingested(state, "학생정보/김준수.yaml")
        self.assertEqual("REPORT_INGESTED", state["status"])
        state = rs.set_structured(state)
        self.assertEqual("STRUCTURED", state["status"])
        state = rs.set_drafted(state, "초안 본문")
        self.assertEqual("DRAFTED", state["status"])
        state = rs.set_awaiting_review(state)
        self.assertEqual("AWAITING_REVIEW", state["status"])
        state = rs.mark_reviewed(state, approved=True, edited_text="수정된 본문", reviewer_note=None)
        self.assertEqual("REVIEWED", state["status"])
        state = rs.set_evaluated(state, "세특/김준수.md")
        self.assertEqual("EVALUATED", state["status"])
        state = rs.set_linted(state, passed=False, retry_count=1, summary="BYTE_LIMIT 1건")
        self.assertEqual("LINTED_FAIL", state["status"])
        state = rs.set_linted(state, passed=True, retry_count=2, summary=None)
        self.assertEqual("LINTED_PASS", state["status"])

    def test_reject_returns_to_review_rejected_and_redraft_is_allowed(self):
        state = rs.new_state("김준수", "A")
        state = rs.set_report_ingested(state, "학생정보/김준수.yaml")
        state = rs.set_structured(state)
        state = rs.set_drafted(state, "초안 본문")
        state = rs.set_awaiting_review(state)
        state = rs.mark_reviewed(state, approved=False, edited_text=None, reviewer_note="탐구 과정을 더 구체적으로")
        self.assertEqual("REVIEW_REJECTED", state["status"])
        state = rs.set_drafted(state, "보강된 초안 본문")
        self.assertEqual("DRAFTED", state["status"])
        self.assertFalse(state["steps"]["review"]["done"])

    def test_set_evaluated_rejected_before_review(self):
        state = rs.new_state("김준수", "A")
        with self.assertRaises(rs.RunStateError):
            rs.set_evaluated(state, "세특/김준수.md")


class ReviewCliTests(unittest.TestCase):
    def _drafted_and_awaiting(self, directory: Path) -> Path:
        path = rs.state_path(directory, "김준수")
        state = rs.new_state("김준수", "A")
        state = rs.set_report_ingested(state, "학생정보/김준수.yaml")
        state = rs.set_structured(state)
        state = rs.set_drafted(state, "AI가 작성한 초안 본문입니다.")
        state = rs.set_awaiting_review(state)
        rs.save_state(path, state)
        return path

    def test_open_writes_review_draft_file(self):
        with tempfile.TemporaryDirectory() as directory:
            run_state_dir = Path(directory)
            self._drafted_and_awaiting(run_state_dir)
            review_cli.cmd_open(run_state_dir, "김준수")
            review_path = rs.review_draft_path(run_state_dir, "김준수")
            self.assertTrue(review_path.exists())
            self.assertEqual("AI가 작성한 초안 본문입니다.", review_path.read_text(encoding="utf-8"))

    def test_approve_moves_to_reviewed_and_removes_review_file(self):
        with tempfile.TemporaryDirectory() as directory:
            run_state_dir = Path(directory)
            state_path = self._drafted_and_awaiting(run_state_dir)
            review_cli.cmd_open(run_state_dir, "김준수")
            review_path = rs.review_draft_path(run_state_dir, "김준수")
            review_path.write_text("교사가 수정한 최종 초안", encoding="utf-8")

            review_cli.cmd_approve(run_state_dir, "김준수", note="문장 하나 다듬음")

            state = rs.load_state(state_path)
            self.assertEqual("REVIEWED", state["status"])
            self.assertTrue(state["steps"]["review"]["approved"])
            self.assertEqual("교사가 수정한 최종 초안", state["steps"]["review"]["edited_text"])
            self.assertFalse(review_path.exists())

    def test_approve_without_open_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            run_state_dir = Path(directory)
            self._drafted_and_awaiting(run_state_dir)
            with self.assertRaises(rs.RunStateError):
                review_cli.cmd_approve(run_state_dir, "김준수", note=None)

    def test_reject_records_reason_and_blocks_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            run_state_dir = Path(directory)
            state_path = self._drafted_and_awaiting(run_state_dir)

            review_cli.cmd_reject(run_state_dir, "김준수", "동기 부분을 더 구체적으로 써주세요")

            state = rs.load_state(state_path)
            self.assertEqual("REVIEW_REJECTED", state["status"])
            self.assertFalse(state["steps"]["review"]["approved"])
            with self.assertRaises(rs.RunStateError):
                rs.set_evaluated(state, "세특/김준수.md")

    def test_status_lists_all_students(self):
        with tempfile.TemporaryDirectory() as directory:
            run_state_dir = Path(directory)
            self._drafted_and_awaiting(run_state_dir)
            output = review_cli.cmd_status(run_state_dir, None)
            self.assertIn("김준수", output)
            self.assertIn("AWAITING_REVIEW", output)


class RunStateCliSmokeTest(unittest.TestCase):
    def test_end_to_end_cli_flow(self):
        python = ROOT / "python_portable" / "python.exe"
        run_state_script = ROOT / "scripts" / "run_state.py"
        review_script = ROOT / "scripts" / "review_cli.py"

        with tempfile.TemporaryDirectory() as directory:
            run_state_dir = Path(directory) / "run_state"
            draft_file = Path(directory) / "draft.md"
            draft_file.write_text("포터블 Python CLI로 만든 초안입니다.", encoding="utf-8")

            def run(*args: str) -> subprocess.CompletedProcess:
                return subprocess.run(
                    [str(python), str(run_state_script), "--dir", str(run_state_dir), *args],
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )

            self.assertEqual(0, run("init", "김준수", "A").returncode)
            self.assertEqual(
                0, run("set-report-ingested", "김준수", "--yaml-path", "학생정보/김준수.yaml").returncode
            )
            self.assertEqual(0, run("set-structured", "김준수").returncode)
            self.assertEqual(
                0, run("set-drafted", "김준수", "--draft-file", str(draft_file)).returncode
            )
            awaiting = run("set-awaiting-review", "김준수")
            self.assertEqual(0, awaiting.returncode, awaiting.stderr)
            self.assertIn("AWAITING_REVIEW", awaiting.stdout)

            opened = subprocess.run(
                [str(python), str(review_script), "--dir", str(run_state_dir), "open", "김준수"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(0, opened.returncode, opened.stderr)

            review_path = run_state_dir / "김준수_review.md"
            self.assertTrue(review_path.exists())

            approved = subprocess.run(
                [str(python), str(review_script), "--dir", str(run_state_dir), "approve", "김준수"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(0, approved.returncode, approved.stderr)

            self.assertEqual(
                0, run("set-evaluated", "김준수", "--output-path", "세특/김준수.md").returncode
            )
            linted = run("set-linted", "김준수", "--result", "pass", "--retry-count", "0")
            self.assertEqual(0, linted.returncode, linted.stderr)

            listing = run("list")
            self.assertIn("LINTED_PASS", listing.stdout)


if __name__ == "__main__":
    unittest.main()
