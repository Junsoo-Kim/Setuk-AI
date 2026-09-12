"""prompt_version()이 실제로 "무엇으로 만들었는가"를 반영하는지 검증한다.

이 값은 run 레코드(`db.py`의 `runs.prompt_version`)에 저장되어, 나중에
"이 초안은 어떤 지침·규칙으로 만들어졌나"를 되짚는 유일한 단서가 된다.
지침(.agent/*.md)이나 규칙(rules.json)이 바뀌었는데 이 값이 그대로면
과거 run과 새 run을 구분할 수 없게 되므로, 두 종류의 변경 모두 값을
바꿔야 한다.

또한 C버전은 `.agent/*.md`를 자기 폴더에 복사해 두지 않고 B버전 원본을
그대로 읽는다 — 두 버전이 서로 다른 사본을 참조하면 "같은 지침으로
만들었다"는 전제 자체가 깨진다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from version_C import prompts


def _clear_caches() -> None:
    prompts.load_prompt.cache_clear()
    prompts.prompt_version.cache_clear()


class SharedOriginTests(unittest.TestCase):
    """C버전이 B버전 지침을 복사하지 않고 동일한 원본에서 로드하는지 확인한다."""

    def test_agent_dir_points_into_version_b_not_a_private_copy(self):
        self.assertEqual(
            prompts.AGENT_DIR,
            ROOT / "version_B" / ".agent",
        )

    def test_rules_path_points_into_version_b_not_a_private_copy(self):
        self.assertEqual(prompts.RULES_PATH, ROOT / "version_B" / "rules.json")

    def test_version_c_does_not_keep_its_own_agent_directory(self):
        # version_C/.agent가 따로 있으면 두 버전이 서로 다른 사본을 참조하게 될 위험이 있다.
        self.assertFalse((ROOT / "version_C" / ".agent").exists())


class PromptVersionChangeTests(unittest.TestCase):
    """지침·규칙이 바뀌면 prompt_version도 함께 바뀌는지 확인한다."""

    def setUp(self):
        self._tmp_agent_patch = None

    def tearDown(self):
        _clear_caches()

    def _make_agent_copy(self, tmp_root: Path) -> Path:
        agent_dir = tmp_root / ".agent"
        agent_dir.mkdir()
        for filename in prompts.VERSIONED_PROMPTS:
            (agent_dir / filename).write_text(f"# {filename} v1\n", encoding="utf-8")
        return agent_dir

    def test_editing_an_agent_instruction_file_changes_the_version(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            agent_dir = self._make_agent_copy(tmp_root)
            rules_path = tmp_root / "rules.json"
            rules_path.write_text("{}", encoding="utf-8")

            with patch.object(prompts, "AGENT_DIR", agent_dir), patch.object(
                prompts, "RULES_PATH", rules_path
            ):
                _clear_caches()
                before = prompts.prompt_version()

                (agent_dir / "02_data_structuring.md").write_text(
                    "# 02_data_structuring.md v2 (수정됨)\n", encoding="utf-8"
                )
                _clear_caches()
                after = prompts.prompt_version()

            self.assertNotEqual(before, after)

    def test_editing_rules_json_changes_the_version(self):
        """분량·금칙어 규칙이 바뀌어도 지침 파일 자체는 그대로일 수 있다.

        이 테스트는 수정 전 prompt_version()에서는 실패한다 — 예전 구현은
        `.agent/*.md` 네 개만 해시에 넣고 rules.json은 보지 않았기 때문에,
        규칙만 바뀐 두 상태가 같은 버전 문자열을 돌려주었다.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            agent_dir = self._make_agent_copy(tmp_root)
            rules_path = tmp_root / "rules.json"
            rules_path.write_text('{"max_bytes": 1500}', encoding="utf-8")

            with patch.object(prompts, "AGENT_DIR", agent_dir), patch.object(
                prompts, "RULES_PATH", rules_path
            ):
                _clear_caches()
                before = prompts.prompt_version()

                rules_path.write_text('{"max_bytes": 1200}', encoding="utf-8")
                _clear_caches()
                after = prompts.prompt_version()

            self.assertNotEqual(before, after)

    def test_identical_content_yields_identical_version(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
            root_a, root_b = Path(tmp_a), Path(tmp_b)
            agent_a = self._make_agent_copy(root_a)
            agent_b = self._make_agent_copy(root_b)
            rules_a = root_a / "rules.json"
            rules_b = root_b / "rules.json"
            rules_a.write_text('{"max_bytes": 1500}', encoding="utf-8")
            rules_b.write_text('{"max_bytes": 1500}', encoding="utf-8")

            with patch.object(prompts, "AGENT_DIR", agent_a), patch.object(
                prompts, "RULES_PATH", rules_a
            ):
                _clear_caches()
                version_a = prompts.prompt_version()

            with patch.object(prompts, "AGENT_DIR", agent_b), patch.object(
                prompts, "RULES_PATH", rules_b
            ):
                _clear_caches()
                version_b = prompts.prompt_version()

            self.assertEqual(version_a, version_b)


if __name__ == "__main__":
    unittest.main()
