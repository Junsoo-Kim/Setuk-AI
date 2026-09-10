"""Claude 호출과 계약 파싱.

v1은 yaml 블록만 뽑아 `contract` 필드 존재만 확인했다. 지금은 단계별 Pydantic
스키마(`contracts.py`)로 검증하고, 실패하면 **한 번만** repair 프롬프트를 보낸다.
두 번 이상 재시도하지 않는 이유는 형식 오류가 두 번 연속 나면 프롬프트나 모델
설정 문제일 가능성이 높고, 무한 재시도는 비용만 늘리기 때문이다.

관측성: 프롬프트·응답 전문은 기본적으로 로그에 남기지 않는다(학생 초안이 들어 있다).
`SETUK_LOG_RAW_LLM=1`일 때만 전문을 남기고, 평소에는 해시·길이·토큰 수만 기록한다.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import yaml
from pydantic import BaseModel

from .contracts import ContractValidationError, parse_stage_contract

_YAML_BLOCK_RE = re.compile(r"```ya?ml\s*\n(.*?)```", re.DOTALL)

DEFAULT_MODEL = "claude-opus-5"
MAX_REPAIR_ATTEMPTS = 1

logger = logging.getLogger("setuk.llm")


class ContractParseError(Exception):
    """yaml 블록 자체를 찾거나 파싱할 수 없는 경우."""


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    model: str = DEFAULT_MODEL
    max_tokens: int = 8000


@dataclass
class CallMetrics:
    """한 단계에서 발생한 호출 비용·실패를 모아 감사 로그로 넘긴다."""

    stage: str
    attempts: int = 0
    repairs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    parse_failures: list[str] = field(default_factory=list)
    response_hashes: list[str] = field(default_factory=list)

    def as_detail(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "attempts": self.attempts,
            "repairs": self.repairs,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "parse_failures": self.parse_failures,
            "response_hashes": self.response_hashes,
        }


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


def log_raw_enabled() -> bool:
    return os.environ.get("SETUK_LOG_RAW_LLM", "").strip() in {"1", "true", "TRUE", "yes"}


def response_fingerprint(raw: str) -> str:
    """응답 전문 대신 남기는 식별자. 같은 실패가 반복되는지 확인할 수 있다."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def call_agent(system_prompt: str, user_content: str, config: LLMConfig) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=config.api_key)
    response = client.messages.create(
        model=config.model,
        max_tokens=config.max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )
    usage = getattr(response, "usage", None)
    if usage is not None:
        _LAST_USAGE["input_tokens"] = getattr(usage, "input_tokens", 0) or 0
        _LAST_USAGE["output_tokens"] = getattr(usage, "output_tokens", 0) or 0
    return "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )


# call_agent를 테스트에서 통째로 대체할 수 있어야 하므로 토큰 수는 모듈 변수로 받는다.
_LAST_USAGE: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}


def parse_contract(raw_text: str) -> dict[str, Any]:
    """응답에서 yaml 계약 블록을 dict로 꺼낸다(스키마 검증 없음)."""
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


def contract_as_text(contract: dict[str, Any] | BaseModel) -> str:
    if isinstance(contract, BaseModel):
        contract = contract.model_dump(exclude_none=True)
    return yaml.safe_dump(contract, allow_unicode=True, sort_keys=False)


def _repair_prompt(stage: str, problems: str, raw: str) -> str:
    excerpt = raw.strip()
    if len(excerpt) > 4000:
        excerpt = excerpt[:4000] + "\n...(이하 생략)"
    return (
        "직전 응답이 계약 검증을 통과하지 못했다. 아래 문제만 고쳐서 **yaml 계약 블록 하나만** "
        "다시 출력하라. 설명 문장, 사과, 코드 블록 밖의 텍스트를 덧붙이지 마라. "
        "사실 자체를 새로 만들지 말고 직전 응답의 내용을 유지한 채 형식과 참조만 고쳐라.\n\n"
        f"검증 실패 항목:\n{problems}\n\n"
        f"직전 응답:\n{excerpt}"
    )


def request_contract(
    system_prompt: str,
    user_content: str,
    config: LLMConfig,
    stage: str,
    *,
    agent: Callable[[str, str, LLMConfig], str] | None = None,
    extra_validator: Callable[[BaseModel], None] | None = None,
    metrics: CallMetrics | None = None,
) -> BaseModel:
    """계약을 요청하고 검증한다. 형식 실패 시 최대 1회 repair 프롬프트를 보낸다.

    `extra_validator`는 단계 간 정합성(명제 ID 참조, 경로 일치)처럼 스키마만으로는
    확인할 수 없는 검증을 넣는 자리다. 여기서 던진 `ContractValidationError`도
    repair 대상이 된다 — 모델이 스스로 고칠 수 있는 종류의 오류이기 때문이다.
    """
    invoke = agent or call_agent
    tracker = metrics or CallMetrics(stage=stage)
    prompt = user_content
    last_error: Exception | None = None

    for attempt in range(MAX_REPAIR_ATTEMPTS + 1):
        started = time.monotonic()
        _LAST_USAGE["input_tokens"] = 0
        _LAST_USAGE["output_tokens"] = 0
        raw = invoke(system_prompt, prompt, config)
        tracker.attempts += 1
        tracker.latency_ms += int((time.monotonic() - started) * 1000)
        tracker.input_tokens += _LAST_USAGE["input_tokens"]
        tracker.output_tokens += _LAST_USAGE["output_tokens"]
        fingerprint = response_fingerprint(raw)
        tracker.response_hashes.append(fingerprint)

        try:
            data = parse_contract(raw)
            contract = parse_stage_contract(data, stage)
            if extra_validator is not None:
                extra_validator(contract)
            return contract
        except (ContractParseError, ContractValidationError) as exc:
            last_error = exc
            problems = str(exc)
            tracker.parse_failures.append(problems)
            if log_raw_enabled():
                logger.warning("[%s] 계약 검증 실패 (원문 로깅 켜짐): %s\n%s", stage, problems, raw)
            else:
                logger.warning(
                    "[%s] 계약 검증 실패 (attempt=%d, response=%s): %s",
                    stage,
                    attempt + 1,
                    fingerprint,
                    problems,
                )
            if attempt >= MAX_REPAIR_ATTEMPTS:
                break
            tracker.repairs += 1
            prompt = _repair_prompt(stage, problems, raw)

    raise ContractValidationError(
        f"{stage}단계 계약 검증이 repair 재시도 후에도 실패했습니다: {last_error}"
    )
