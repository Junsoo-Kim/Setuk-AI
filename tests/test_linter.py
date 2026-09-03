import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import linter


def make_rules(**overrides):
    rules = {
        "schema_version": 1,
        "max_bytes": 1500,
        "target_min_bytes": 0,
        "byte_count": {"ascii": 1, "non_ascii": 3, "line_break": 2},
        "allowed_punctuation": ".,·'\"-()[]/%+=:;?!&",
        "allowed_symbols": "℃°±×÷",
        "forbidden_terms": [],
    }
    rules.update(overrides)
    return rules


class ByteCountTests(unittest.TestCase):
    def test_counts_ascii_korean_and_line_breaks(self):
        byte_rules = make_rules()["byte_count"]
        self.assertEqual(linter.neis_byte_count("A한\nB", byte_rules), 7)
        self.assertEqual(linter.neis_byte_count("A한\r\nB", byte_rules), 7)


class LintTests(unittest.TestCase):
    def test_valid_text_passes(self):
        result = linter.lint_text("탐구 결과를 근거로 가설을 수정함.", "student.md", make_rules())
        self.assertTrue(result.passed)
        self.assertEqual(result.errors, 0)

    def test_byte_limit_is_an_error(self):
        result = linter.lint_text("한글", "student.md", make_rules(max_bytes=5))
        self.assertFalse(result.passed)
        self.assertIn("BYTE_LIMIT", [item.code for item in result.diagnostics])

    def test_below_target_length_is_a_warning(self):
        result = linter.lint_text(
            "탐구함.", "student.md", make_rules(target_min_bytes=100)
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.warnings, 1)
        self.assertIn("BELOW_TARGET_LENGTH", [item.code for item in result.diagnostics])
        self.assertEqual(result.target_min_bytes, 100)

    def test_forbidden_term_is_case_insensitive(self):
        rules = make_rules(
            forbidden_terms=[{"term": "TOEIC", "reason": "기재 제한 여부 확인"}]
        )
        result = linter.lint_text("toeic 점수를 기록함.", "student.md", rules)
        diagnostic = next(item for item in result.diagnostics if item.code == "FORBIDDEN_TERM")
        self.assertEqual((diagnostic.line, diagnostic.column), (1, 1))

    def test_unsupported_character_is_an_error(self):
        result = linter.lint_text("탐구함😀", "student.md", make_rules())
        self.assertIn("UNSUPPORTED_CHARACTER", [item.code for item in result.diagnostics])

    def test_tab_is_an_error(self):
        result = linter.lint_text("탐구\t결과", "student.md", make_rules())
        self.assertIn("TAB_CHARACTER", [item.code for item in result.diagnostics])

    def test_empty_content_is_an_error(self):
        result = linter.lint_text("  \n", "student.md", make_rules())
        self.assertIn("EMPTY_CONTENT", [item.code for item in result.diagnostics])


class ConfigTests(unittest.TestCase):
    def test_load_rules_rejects_invalid_max_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            path.write_text(json.dumps(make_rules(max_bytes=0)), encoding="utf-8")
            with self.assertRaises(linter.LinterError):
                linter.load_rules(path)

    def test_load_rules_rejects_target_min_above_max(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            path.write_text(
                json.dumps(make_rules(target_min_bytes=1501)), encoding="utf-8"
            )
            with self.assertRaises(linter.LinterError):
                linter.load_rules(path)


class CliTests(unittest.TestCase):
    def test_json_output_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            valid_path = Path(directory) / "valid.md"
            invalid_path = Path(directory) / "invalid.md"
            valid_path.write_text("수업 내용을 분석함.", encoding="utf-8")
            invalid_path.write_text("수업 내용을 분석함😀", encoding="utf-8")

            valid = subprocess.run(
                [sys.executable, str(ROOT / "linter.py"), str(valid_path), "--json"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            invalid = subprocess.run(
                [sys.executable, str(ROOT / "linter.py"), str(invalid_path), "--json"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )

            self.assertEqual(valid.returncode, 0)
            self.assertTrue(json.loads(valid.stdout)["passed"])
            self.assertEqual(invalid.returncode, 1)
            self.assertFalse(json.loads(invalid.stdout)["passed"])


if __name__ == "__main__":
    unittest.main()
