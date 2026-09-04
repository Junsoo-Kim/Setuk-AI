import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent


class ReleaseLayoutTests(unittest.TestCase):
    def test_required_release_sources_exist(self):
        required = [
            "AGENTS.md",
            ".agent/01_report_ingestion.md",
            ".agent/02_data_structuring.md",
            ".agent/03_drafting.md",
            ".agent/04_evaluation.md",
            ".clinerules/00-setuk-master.md",
            ".clinerules/workflows/setuk.md",
            "학생정보/template.yaml",
            "보고서/README.md",
            "세특/README.md",
            "창체/README.md",
            "run_state/README.md",
            "python_portable/python.exe",
            "python_portable/_socket.pyd",
            "python_portable/LICENSE.txt",
            "linter.py",
            "scripts/extract_docx.py",
            "scripts/run_state.py",
            "scripts/review_cli.py",
            "rules.json",
            "사용안내.md",
            "THIRD_PARTY_NOTICES.md",
            "VERSION",
            "scripts/create_release_zip.py",
        ]
        missing = [path for path in required if not (ROOT / path).exists()]
        self.assertEqual([], missing)

    def test_portable_python_runs(self):
        completed = subprocess.run(
            [str(ROOT / "python_portable" / "python.exe"), "--version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("Python 3.13.15", completed.stdout)

    def test_documented_commands_use_release_runtime_path(self):
        for relative_path in ("README.md", "AGENTS.md", "사용안내.md"):
            content = (ROOT / relative_path).read_text(encoding="utf-8")
            self.assertNotIn("python-3.13.15-embed-amd64", content)
            self.assertIn("python_portable", content)

    def test_version_is_semantic(self):
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        parts = version.split(".")
        self.assertEqual(3, len(parts))
        self.assertTrue(all(part.isdigit() for part in parts))

    def test_portable_extension_modules_are_not_ignored(self):
        ignore_rules = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("!version_B/python_portable/*.pyd", ignore_rules)

    def test_build_manifest_does_not_copy_student_directories_wholesale(self):
        script = (ROOT / "scripts" / "build_release.ps1").read_text(encoding="utf-8")
        self.assertNotIn("'학생정보',", script)
        self.assertNotIn("'세특',", script)
        self.assertIn("'학생정보/template.yaml'", script)
        self.assertIn("'학생정보/example.yaml'", script)
        self.assertIn("'세특/README.md'", script)
        self.assertNotIn("'보고서',", script)
        self.assertIn("'보고서/README.md'", script)
        self.assertNotIn("'창체',", script)
        self.assertIn("'창체/README.md'", script)
        self.assertNotIn("'run_state',", script)
        self.assertIn("'run_state/README.md'", script)

    def test_private_reports_are_ignored(self):
        ignore_rules = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("version_B/보고서/*", ignore_rules)
        self.assertIn("!version_B/보고서/README.md", ignore_rules)

    def test_activity_and_run_state_outputs_are_ignored(self):
        ignore_rules = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("version_B/창체/*.md", ignore_rules)
        self.assertIn("!version_B/창체/README.md", ignore_rules)
        self.assertIn("version_B/run_state/*.json", ignore_rules)
        self.assertIn("!version_B/run_state/README.md", ignore_rules)

    def test_zip_builder_uses_portable_entry_separators(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            source = temporary / "staging"
            nested = source / "Setuk-Harness-test" / "학생정보"
            nested.mkdir(parents=True)
            (nested / "template.yaml").write_text("schema_version: 1\n", encoding="utf-8")
            archive = temporary / "release.zip"
            completed = subprocess.run(
                [
                    str(ROOT / "python_portable" / "python.exe"),
                    str(ROOT / "scripts" / "create_release_zip.py"),
                    str(source),
                    str(archive),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            with zipfile.ZipFile(archive) as release:
                self.assertEqual(
                    ["Setuk-Harness-test/학생정보/template.yaml"], release.namelist()
                )


if __name__ == "__main__":
    unittest.main()
