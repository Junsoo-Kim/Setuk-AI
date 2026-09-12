from __future__ import annotations

import hashlib
from functools import lru_cache

from .bridge import ROOT

AGENT_DIR = ROOT / ".agent"
RULES_PATH = ROOT / "rules.json"

# 어떤 지침으로 만든 결과인지 run에 기록하기 위해 버전을 계산하는 대상.
VERSIONED_PROMPTS = (
    "01_report_ingestion.md",
    "02_data_structuring.md",
    "03_drafting.md",
    "04_evaluation.md",
)


@lru_cache(maxsize=None)
def load_prompt(filename: str) -> str:
    path = AGENT_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(f"지침 파일을 찾을 수 없습니다: {path}")
    return path.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def prompt_version() -> str:
    """파이프라인이 쓰는 지침·규칙 묶음의 버전 해시.

    `.agent/*.md`는 B버전과 공유하는 파일이라 B버전 쪽 개정이 C버전 결과를 바꾼다.
    `rules.json`(분량·금칙어 등 Linter 규칙)도 초안의 표현과 분량 판단에 직접 영향을
    주므로 지침과 함께 해시에 포함한다. 나중에 "이 초안은 어떤 지침·규칙으로
    만들어졌나"를 되짚으려면 run마다 이 값을 남겨야 한다.
    """
    digest = hashlib.sha256()
    for filename in VERSIONED_PROMPTS:
        path = AGENT_DIR / filename
        digest.update(filename.encode("utf-8"))
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
    digest.update(b"rules.json")
    digest.update(RULES_PATH.read_bytes() if RULES_PATH.is_file() else b"<missing>")
    return f"agent-{digest.hexdigest()[:12]}"
