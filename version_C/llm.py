from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

import yaml

_YAML_BLOCK_RE = re.compile(r"```ya?ml\s*\n(.*?)```", re.DOTALL)

DEFAULT_MODEL = "claude-opus-5"


class ContractParseError(Exception):
    pass


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    model: str = DEFAULT_MODEL
    max_tokens: int = 8000


def load_config() -> LLMConfig:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY가 설정되지 않았습니다. "
            "version_C/.env.example을 version_C/.env로 복사하고 발급받은 키를 넣으세요."
        )
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
    max_tokens = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "8000"))
    return LLMConfig(api_key=api_key, model=model, max_tokens=max_tokens)


def call_agent(system_prompt: str, user_content: str, config: LLMConfig) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=config.api_key)
    response = client.messages.create(
        model=config.model,
        max_tokens=config.max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )
    return "".join(block.text for block in response.content if getattr(block, "type", None) == "text")


def parse_contract(raw_text: str) -> dict[str, Any]:
    match = _YAML_BLOCK_RE.search(raw_text)
    if not match:
        raise ContractParseError("응답에서 yaml 계약 블록을 찾을 수 없습니다.")
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ContractParseError(f"yaml 계약 블록을 파싱할 수 없습니다: {exc}") from exc
    if not isinstance(data, dict) or "contract" not in data:
        raise ContractParseError("yaml 계약 블록에 contract 필드가 없습니다.")
    return data


def contract_as_text(contract: dict[str, Any]) -> str:
    return yaml.safe_dump(contract, allow_unicode=True, sort_keys=False)
