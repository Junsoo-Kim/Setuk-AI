"""배포 ZIP을 실제로 만들어서 그 내용물 자체를 검사한다.

`test_release_layout.py`는 `build_release.ps1`의 **문구**(어떤 경로를 나열했는지)만
확인한다. 스크립트가 개인정보나 임시 상태 파일을 실수로 통째로 복사하도록 바뀌어도,
문구 검사만으로는 잡지 못하는 경우가 있다 — 예를 들어 화이트리스트에 없는 경로를
와일드카드로 새로 추가하면 문구 검사는 그 새 규칙만 보고 통과시킬 수 있다.

이 테스트는 `build_release.ps1`이 선언한 화이트리스트를 그대로 읽어 실제로 스테이징
폴더를 만들고 `create_release_zip.py`로 ZIP을 만든 다음, 그 ZIP의 실제 엔트리 목록을
검사한다. 전체 자동 테스트 실행(`build_release.ps1`이 이미 함)은 되풀이하지 않고,
산출물 자체에만 집중한다.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts" / "build_release.ps1"

FORBIDDEN_SUFFIXES = (".env", ".sqlite3", ".sqlite", ".log", ".db")

_ARRAY_RE = re.compile(r"\$(releaseDirectories|releaseFiles)\s*=\s*@\((.*?)\)", re.DOTALL)
_ITEM_RE = re.compile(r"'([^']+)'")


def _parse_whitelist() -> tuple[list[str], list[str]]:
    """build_release.ps1의 $releaseDirectories / $releaseFiles 배열을 그대로 읽는다.

    화이트리스트를 이 테스트에 다시 옮겨 적지 않는 이유는, 그러면 스크립트만 바뀌고
    테스트가 낡은 사본을 계속 검사하는 사고가 나기 때문이다.
    """
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    arrays: dict[str, list[str]] = {}
    for match in _ARRAY_RE.finditer(script):
        name, body = match.group(1), match.group(2)
        arrays[name] = _ITEM_RE.findall(body)
    directories = arrays.get("releaseDirectories")
    files = arrays.get("releaseFiles")
    if not directories or not files:
        raise AssertionError("build_release.ps1에서 화이트리스트 배열을 찾을 수 없습니다.")
    return directories, files


class _BuiltArtifact:
    """실제 화이트리스트로 스테이징 폴더를 만들고 ZIP까지 만든다 (전체 테스트는 재실행하지 않음)."""

    PACKAGE_NAME = "Setuk-Harness-artifact-test"

    def __enter__(self):
        directories, files = _parse_whitelist()
        self._tmp = tempfile.TemporaryDirectory()
        tmp_root = Path(self._tmp.name)
        staging_root = tmp_root / "staging"
        package_root = staging_root / self.PACKAGE_NAME
        package_root.mkdir(parents=True)

        for relative in directories:
            shutil.copytree(ROOT / relative, package_root / relative)
        for relative in files:
            source = ROOT / relative
            destination = package_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        self.archive_path = tmp_root / f"{self.PACKAGE_NAME}.zip"
        completed = subprocess.run(
            [
                str(ROOT / "python_portable" / "python.exe"),
                str(ROOT / "scripts" / "create_release_zip.py"),
                str(staging_root),
                str(self.archive_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if completed.returncode != 0:
            raise AssertionError(f"ZIP 생성 실패: {completed.stderr}")

        self.namelist = zipfile.ZipFile(self.archive_path).namelist()
        return self

    def entries_under(self, relative_dir: str) -> list[str]:
        prefix = f"{self.PACKAGE_NAME}/{relative_dir}/"
        return [name for name in self.namelist if name.startswith(prefix)]

    def __exit__(self, *exc):
        self._tmp.cleanup()


class ForbiddenContentTests(unittest.TestCase):
    """실제 학생 파일·비밀·임시 상태가 배포 ZIP에 들어가지 않는지 확인한다."""

    def test_no_env_or_database_or_log_files(self):
        with _BuiltArtifact() as artifact:
            offending = [
                name
                for name in artifact.namelist
                if name.lower().endswith(FORBIDDEN_SUFFIXES)
            ]
            self.assertEqual([], offending)

    def test_student_info_only_contains_template_and_example(self):
        with _BuiltArtifact() as artifact:
            yaml_entries = [
                name
                for name in artifact.entries_under("학생정보")
                if name.endswith(".yaml")
            ]
            basenames = {name.rsplit("/", 1)[-1] for name in yaml_entries}
            self.assertEqual({"template.yaml", "example.yaml"}, basenames)

    def test_no_real_student_output_files(self):
        with _BuiltArtifact() as artifact:
            md_entries = [
                name for name in artifact.entries_under("세특") if name.endswith(".md")
            ]
            basenames = {name.rsplit("/", 1)[-1] for name in md_entries}
            self.assertEqual({"README.md"}, basenames)

    def test_no_leftover_run_state_checkpoints(self):
        with _BuiltArtifact() as artifact:
            json_entries = [
                name
                for name in artifact.entries_under("run_state")
                if name.endswith(".json")
            ]
            self.assertEqual([], json_entries)

    def test_no_activity_output_files_outside_readme(self):
        with _BuiltArtifact() as artifact:
            md_entries = [
                name for name in artifact.entries_under("창체") if name.endswith(".md")
            ]
            basenames = {name.rsplit("/", 1)[-1] for name in md_entries}
            self.assertEqual({"README.md"}, basenames)


class UnpackedArtifactSmokeTests(unittest.TestCase):
    """ZIP을 실제로 풀어서 그 안의 포터블 Python과 Linter가 동작하는지 확인한다."""

    def test_unpacked_portable_python_runs(self):
        with _BuiltArtifact() as artifact:
            with tempfile.TemporaryDirectory() as extract_dir:
                with zipfile.ZipFile(artifact.archive_path) as archive:
                    archive.extractall(extract_dir)
                package_root = Path(extract_dir) / artifact.PACKAGE_NAME
                completed = subprocess.run(
                    [str(package_root / "python_portable" / "python.exe"), "--version"],
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
                self.assertEqual(0, completed.returncode, completed.stderr)

    def test_unpacked_linter_checks_example_input(self):
        with _BuiltArtifact() as artifact:
            with tempfile.TemporaryDirectory() as extract_dir:
                with zipfile.ZipFile(artifact.archive_path) as archive:
                    archive.extractall(extract_dir)
                package_root = Path(extract_dir) / artifact.PACKAGE_NAME

                sample = package_root / "세특" / ".artifact-smoke.md"
                sample.write_text(
                    "수업에서 수집한 자료를 비교하고 결과를 근거로 결론을 수정함.",
                    encoding="utf-8",
                )
                completed = subprocess.run(
                    [
                        str(package_root / "python_portable" / "python.exe"),
                        str(package_root / "linter.py"),
                        str(sample),
                        "--json",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                )
                self.assertEqual(0, completed.returncode, completed.stderr)


if __name__ == "__main__":
    unittest.main()
