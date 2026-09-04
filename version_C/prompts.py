from __future__ import annotations

from functools import lru_cache

from .bridge import ROOT

AGENT_DIR = ROOT / ".agent"


@lru_cache(maxsize=None)
def load_prompt(filename: str) -> str:
    path = AGENT_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(f"지침 파일을 찾을 수 없습니다: {path}")
    return path.read_text(encoding="utf-8")
