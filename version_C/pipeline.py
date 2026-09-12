from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol, TypedDict

import yaml
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from . import contracts, privacy
from .bridge import ROOT, extract_docx, linter
from .contracts import ContractValidationError, StructuredFactsV1
from .llm import (
    CallMetrics,
    ContractParseError,
    contract_as_text,
    load_config,
    request_contract,
)
from .prompts import load_prompt, prompt_version

MAX_LINT_RETRIES = 5

# 학생 YAML에서 모델로 나가기 전에 가려야 하는 필드.
_PII_FIELDS = (
    ("student", "name", "학생"),
    ("student", "number", "학번"),
    ("subject", "teacher", "교사"),
)


class PathSafetyError(Exception):
    pass


def _sanitize_student_key(student_key: str) -> str:
    if not student_key or "/" in student_key or "\\" in student_key or ".." in student_key:
        raise PathSafetyError(f"학생 식별자에 허용되지 않은 문자가 있습니다: {student_key!r}")
    return student_key


def _resolve_within(rel_path: str | None, allowed_dir: str, allowed_suffix: str) -> Path:
    if not rel_path:
        raise PathSafetyError("경로가 비어 있습니다.")
    allowed_root = (ROOT / allowed_dir).resolve()
    candidate = (ROOT / rel_path).resolve()
    try:
        candidate.relative_to(allowed_root)
    except ValueError as exc:
        raise PathSafetyError(
            f"'{allowed_dir}/' 밖을 가리키는 경로는 허용하지 않습니다: {rel_path}"
        ) from exc
    if candidate.suffix.casefold() != allowed_suffix.casefold():
        raise PathSafetyError(f"확장자가 {allowed_suffix}가 아닙니다: {rel_path}")
    return candidate


class PipelineState(TypedDict, total=False):
    student_key: str
    pseudonym: str
    mode: str
    report_path: str
    yaml_path: str
    yaml_content: str
    structured_facts: dict[str, Any]
    structured_facts_yaml: str
    draft_text: str
    draft_hash: str
    review_reason: str | None
    reviewed_text: str
    final_text: str
    output_path: str
    lint_retry_count: int
    lint_passed: bool
    lint_summary: str
    lint_diagnostics: list[dict[str, Any]]
    policy_findings: list[dict[str, Any]]
    policy_index_version: str
    policy_retrieval_mode: str
    policy_year: int | None
    needs_input: dict[str, Any] | None
    warnings: list[str]
    metrics: list[dict[str, Any]]
    prompt_version: str
    error: str | None


class PseudonymStore(Protocol):
    """실명 ↔ 별칭 치환표의 저장소.

    이 매핑을 LangGraph의 `PipelineState`에 넣으면 체크포인트 블롭 안에 원본 데이터
    (실명)와 매핑이 나란히 들어간다 — 실명이 API로 안 나간다는 원래 목표는 지키지만,
    "원본 데이터와 가명 매핑을 분리 저장한다"는 요건은 지키지 못한다. 그래서 매핑은
    그래프 상태 밖, 별도 저장소에 둔다.
    """

    def save(self, run_id: str, mapping: dict[str, str]) -> None: ...
    def load(self, run_id: str) -> dict[str, str]: ...


class InMemoryPseudonymStore:
    """DB 없이 동작하는 기본값. 프로세스 하나 안에서만 유효하다."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, str]] = {}

    def save(self, run_id: str, mapping: dict[str, str]) -> None:
        self._data[run_id] = dict(mapping)

    def load(self, run_id: str) -> dict[str, str]:
        return dict(self._data.get(run_id, {}))


DIRECT_CALL_RUN_ID = "__direct_call__"

_pseudonym_store_instance: PseudonymStore = InMemoryPseudonymStore()


def configure_pseudonym_store(store: PseudonymStore) -> None:
    """서버가 기동 시 DB 기반 저장소로 교체한다. 안 부르면 인메모리 기본값을 쓴다."""
    global _pseudonym_store_instance
    _pseudonym_store_instance = store


def _pseudonym_store() -> PseudonymStore:
    return _pseudonym_store_instance


def _run_id(state: PipelineState, config: RunnableConfig | None) -> str:
    """이 실행의 식별자.

    LangGraph의 `config.configurable.thread_id` 주입은 그래프 구조·노드 시그니처
    변화에 취약해 신뢰하지 않는다. 대신 `node_ingest`가 state에 남기는 `pseudonym`
    (학생 식별자에서 결정적으로 계산되는 값)을 키로 쓴다 — 같은 학생은 항상 같은 키,
    다른 학생은 항상 다른 키를 받는다.
    """
    pseudonym = state.get("pseudonym")
    if pseudonym:
        return str(pseudonym)
    return DIRECT_CALL_RUN_ID


def _pseudonymizer(state: PipelineState, config: RunnableConfig | None) -> privacy.Pseudonymizer:
    """이 실행의 치환표로 가명화기를 복원한다.

    실명은 로컬 DB/체크포인트에만 남고 API로는 나가지 않는다. 매핑 자체는 그래프
    상태가 아니라 `_pseudonym_store()`가 관리하는 별도 저장소에서 읽는다.
    """
    mapping = _pseudonym_store().load(_run_id(state, config))
    return privacy.Pseudonymizer.from_mapping(mapping)


def _collect_identifiers(student_key: str, yaml_content: str) -> dict[str, str]:
    """학생 YAML에서 실명·학번·교사명을 찾아 별칭을 붙인다."""
    mapper = privacy.Pseudonymizer()
    mapper.register(student_key, "학생")
    try:
        parsed = yaml.safe_load(yaml_content)
    except yaml.YAMLError:
        parsed = None
    if isinstance(parsed, dict):
        for section, key, role in _PII_FIELDS:
            block = parsed.get(section)
            if isinstance(block, dict):
                value = block.get(key)
                if isinstance(value, str) and value.strip():
                    mapper.register(value, role)
    return mapper.as_mapping()


def _halted(state: PipelineState) -> bool:
    """이 실행이 이미 멈췄는가.

    `error`는 코드가 처리할 수 없는 고장이고 `needs_input`은 사람이 채워야 하는 공백이다.
    둘 다 이후 단계를 건너뛰어야 하므로 판정을 한곳에 모은다.
    """
    return bool(state.get("error") or state.get("needs_input"))


def _needs_input(
    stage: str,
    kind: str,
    *,
    questions: list[dict[str, Any]] | None = None,
    problems: str | None = None,
) -> dict[str, Any]:
    """작업을 죽이지 않고 교사에게 넘긴다.

    계약 복구가 실패했을 때 `error`로 끝내면 교사는 무엇이 부족했는지 못 보고 작업만
    사라진다. Human-in-the-Loop 도구에서 그건 잘못된 기본값이라, 실패 사유를 들고
    검토 상태로 넘긴다.

    `kind`는 호출부가 명시한다. questions 목록이 비었는지로 추론하면, 모델이
    `NEEDS_INPUT_V1`을 빈 질문 목록과 함께 돌려줬을 때 정보 부족이 형식 실패로
    잘못 분류된다.
    """
    return {
        "needs_input": {
            "stage": stage,
            "kind": kind,
            "questions": questions or [],
            "problems": problems,
        }
    }


def _merge_metrics(state: PipelineState, tracker: CallMetrics) -> list[dict[str, Any]]:
    return [*(state.get("metrics") or []), tracker.as_detail()]


def _tokens_used(state: PipelineState) -> int:
    return sum(
        int(item.get("input_tokens") or 0) + int(item.get("output_tokens") or 0)
        for item in (state.get("metrics") or [])
    )


def token_budget() -> int:
    """실행 한 건이 쓸 수 있는 토큰 상한. 0이면 제한하지 않는다.

    반려-재작성 루프와 Linter 재시도 루프가 겹치면 한 학생에 LLM을 십수 번 부를 수 있다.
    교사 개인 API 키로 도는 구조라 예상치 못한 청구서가 곧 서비스 중단으로 이어지므로,
    상한을 넘으면 오류로 멈춰 교사가 상황을 보고 다시 시작하게 한다.
    """
    raw = os.environ.get("SETUK_MAX_TOKENS_PER_RUN", "").strip()
    return int(raw) if raw.isdigit() else 0


def _budget_exceeded(state: PipelineState) -> str | None:
    limit = token_budget()
    if not limit:
        return None
    used = _tokens_used(state)
    if used < limit:
        return None
    return (
        f"이 작업이 토큰 상한({limit:,})을 넘었습니다(사용 {used:,}). "
        "반려 재작성이나 Linter 재시도가 반복되고 있을 수 있습니다. "
        "초안을 직접 확인한 뒤 새로 시작하거나 SETUK_MAX_TOKENS_PER_RUN을 조정하세요."
    )


def node_ingest(state: PipelineState, config: RunnableConfig | None = None) -> dict[str, Any]:
    if _halted(state):
        return {}

    try:
        student_key = _sanitize_student_key(state["student_key"])
    except PathSafetyError as exc:
        return {"error": str(exc)}

    pseudonym = privacy.pseudonym_for(student_key)
    run_id = pseudonym if pseudonym else _run_id(state, config)
    base: dict[str, Any] = {
        "pseudonym": pseudonym,
        "prompt_version": prompt_version(),
    }

    if state["mode"] == "yaml":
        try:
            yaml_path = _resolve_within(state.get("yaml_path"), "학생정보", ".yaml")
        except PathSafetyError as exc:
            return {"error": str(exc)}
        if not yaml_path.is_file():
            return {"error": f"YAML 파일을 찾을 수 없습니다: {yaml_path}"}
        yaml_content = yaml_path.read_text(encoding="utf-8-sig")
        _pseudonym_store().save(run_id, _collect_identifiers(student_key, yaml_content))
        return {
            **base,
            "yaml_path": state["yaml_path"],
            "yaml_content": yaml_content,
        }

    try:
        report_path = _resolve_within(state.get("report_path"), "보고서", ".docx")
    except PathSafetyError as exc:
        return {"error": str(exc)}
    report_path_rel = state["report_path"]
    try:
        extracted = extract_docx.extract_docx(report_path)
    except extract_docx.DocxExtractionError as exc:
        return {"error": f"DOCX 추출 실패: {exc}"}

    # 보고서 본문은 교사가 아니라 학생이 쓴 데이터다. 숨은 지시를 제거하고 경계로 감싼다.
    safe_body, injection_warnings = privacy.sanitize_document(extracted["text"])

    mapper = privacy.Pseudonymizer()
    alias = mapper.register(student_key, "학생")
    tracker = CallMetrics(stage="ingest")

    system_prompt = load_prompt("01_report_ingestion.md")
    user_content = (
        f"학생 식별자: {alias}\n"
        f"보고서 경로: {report_path_rel}\n\n"
        "다음은 scripts/extract_docx.py로 추출한 보고서 본문이다(문단 순서 보존).\n\n"
        f"{mapper.mask(safe_body)}\n\n"
        "위 지침을 그대로 따라 학생 YAML을 만들어라. 이 코드가 파일 저장을 대신 하므로, "
        "REPORT_INGESTED_V1 계약에 지침이 정의한 필드 외에 `yaml_body` 필드(저장할 YAML 파일"
        " 전체 내용, 문자열 하나)를 추가로 포함해 yaml 계약 블록 하나로만 응답하라. "
        "추가 정보가 필요하면 REPORT_NEEDS_INPUT_V1로 응답하라."
    )
    try:
        contract = request_contract(
            system_prompt, user_content, load_config(), "ingest", metrics=tracker
        )
    except (ContractParseError, ContractValidationError) as exc:
        return {
            **base,
            **_needs_input("01_report_ingestion", "contract_validation", problems=str(exc)),
            "warnings": injection_warnings,
            "metrics": _merge_metrics(state, tracker),
        }

    if isinstance(contract, contracts.ReportNeedsInputV1):
        questions = [item.model_dump() for item in contract.questions]
        return {
            **base,
            **_needs_input("01_report_ingestion", "questions", questions=questions),
            "warnings": injection_warnings,
            "metrics": _merge_metrics(state, tracker),
        }

    yaml_path_rel = contract.yaml_path
    # 모델이 별칭으로 만든 YAML을 로컬에 저장할 때만 실명으로 되돌린다.
    yaml_body = mapper.unmask(contract.yaml_body)

    try:
        saved_path = _resolve_within(yaml_path_rel, "학생정보", ".yaml")
    except PathSafetyError as exc:
        return {
            **base,
            "error": f"01단계가 반환한 yaml_path가 안전하지 않습니다: {exc}",
            "metrics": _merge_metrics(state, tracker),
        }

    saved_path.parent.mkdir(parents=True, exist_ok=True)
    saved_path.write_text(yaml_body, encoding="utf-8", newline="\n")
    _pseudonym_store().save(run_id, _collect_identifiers(student_key, yaml_body))
    return {
        **base,
        "yaml_path": yaml_path_rel,
        "yaml_content": yaml_body,
        "warnings": injection_warnings,
        "metrics": _merge_metrics(state, tracker),
    }


def node_structure(state: PipelineState, config: RunnableConfig | None = None) -> dict[str, Any]:
    if _halted(state):
        return {}
    over = _budget_exceeded(state)
    if over:
        return {"error": over}
    mapper = _pseudonymizer(state, config)
    tracker = CallMetrics(stage="structure")
    system_prompt = load_prompt("02_data_structuring.md")
    user_content = (
        "다음은 입력 학생 YAML의 전체 내용이다(별도로 파일을 다시 읽지 말고 이 내용을 그대로"
        " 입력으로 사용하라):\n\n"
        f"```yaml\n{mapper.mask(state['yaml_content'])}\n```\n\n"
        "위 지침을 그대로 따라 STRUCTURED_FACTS_V1 또는 NEEDS_INPUT_V1 yaml 계약 블록 하나로만"
        " 응답하라."
    )
    try:
        contract = request_contract(
            system_prompt, user_content, load_config(), "structure", metrics=tracker
        )
    except (ContractParseError, ContractValidationError) as exc:
        return {
            **_needs_input("02_data_structuring", "contract_validation", problems=str(exc)),
            "metrics": _merge_metrics(state, tracker),
        }

    if isinstance(contract, contracts.NeedsInputV1):
        questions = [item.model_dump() for item in contract.questions]
        return {
            **_needs_input("02_data_structuring", "questions", questions=questions),
            "metrics": _merge_metrics(state, tracker),
        }

    facts = contract.model_dump(exclude_none=True)
    return {
        "structured_facts": facts,
        "structured_facts_yaml": contract_as_text(facts),
        "metrics": _merge_metrics(state, tracker),
    }


def _facts_from_state(state: PipelineState) -> StructuredFactsV1 | None:
    raw = state.get("structured_facts")
    if not raw:
        return None
    return StructuredFactsV1.model_validate(raw)


def node_draft(state: PipelineState, config: RunnableConfig | None = None) -> dict[str, Any]:
    if _halted(state):
        return {}
    over = _budget_exceeded(state)
    if over:
        return {"error": over}
    facts = _facts_from_state(state)
    mapper = _pseudonymizer(state, config)
    tracker = CallMetrics(stage="draft")
    system_prompt = load_prompt("03_drafting.md")
    reason = state.get("review_reason")
    revision_note = (
        f"\n\n이전 교사 검토에서 반려되었다. 다음 사유를 반영해 다시 작성하라: "
        f"{mapper.mask(reason)}"
        if reason
        else ""
    )
    user_content = (
        "다음은 직전 단계의 STRUCTURED_FACTS_V1이다:\n\n"
        f"```yaml\n{state['structured_facts_yaml']}```\n"
        f"{revision_note}\n\n"
        "위 지침을 그대로 따라 DRAFT_V2 yaml 계약 블록 하나로만 응답하라. "
        "text를 문장 단위로 나눠 sentences 배열의 각 항목에 그 문장이 근거로 삼은 "
        "명제 ID를 source_ids로 적어라."
    )

    def check(contract):
        if facts is not None:
            contracts.validate_draft_against_facts(contract, facts)

    try:
        contract = request_contract(
            system_prompt,
            user_content,
            load_config(),
            "draft",
            extra_validator=check,
            metrics=tracker,
        )
    except (ContractParseError, ContractValidationError) as exc:
        return {
            **_needs_input("03_drafting", "contract_validation", problems=str(exc)),
            "metrics": _merge_metrics(state, tracker),
        }

    draft_text = mapper.unmask(contract.text)
    return {
        "draft_text": draft_text,
        "draft_hash": privacy.content_hash(draft_text),
        "review_reason": None,
        "metrics": _merge_metrics(state, tracker),
    }


def node_human_review(state: PipelineState) -> dict[str, Any]:
    if _halted(state):
        return {}
    decision = interrupt(
        {
            "type": "draft_review",
            "student_key": state["student_key"],
            "draft_text": state["draft_text"],
            "draft_hash": state.get("draft_hash"),
        }
    )
    if decision.get("approved"):
        edited = (decision.get("edited_text") or "").strip()
        approved_text = edited or state["draft_text"]
        return {
            "reviewed_text": approved_text,
            "draft_hash": privacy.content_hash(approved_text),
            "review_reason": None,
        }
    reason = (decision.get("reason") or "").strip() or "사유 없음"
    return {"reviewed_text": None, "review_reason": reason}


def route_after_review(state: PipelineState) -> str:
    if _halted(state):
        return "end"
    if state.get("reviewed_text"):
        return "evaluate"
    return "draft"


def node_evaluate(state: PipelineState, config: RunnableConfig | None = None) -> dict[str, Any]:
    if _halted(state):
        return {}
    over = _budget_exceeded(state)
    if over:
        return {"error": over}
    try:
        student_key = _sanitize_student_key(state["student_key"])
    except PathSafetyError as exc:
        return {"error": str(exc)}
    output_path_rel = f"세특/{student_key}.md"
    facts = _facts_from_state(state)
    mapper = _pseudonymizer(state, config)
    tracker = CallMetrics(stage="evaluate")
    system_prompt = load_prompt("04_evaluation.md")
    user_content = (
        "다음은 STRUCTURED_FACTS_V1이다:\n\n"
        f"```yaml\n{state['structured_facts_yaml']}```\n\n"
        "다음은 교사가 검토·승인한 DRAFT_V2.text이다(이 텍스트가 이번 평가·수정의 대상이다):\n\n"
        f"{mapper.mask(state['reviewed_text'])}\n\n"
        f"output_path는 '{output_path_rel}'로 고정한다.\n\n"
        "위 지침을 그대로 따라 평가·수정하라. 이 코드가 파일 저장을 대신 하므로, "
        "EVALUATED_RESULT_V1 계약에 지침이 정의한 필드 외에 `final_text` 필드(저장할 전체 본문,"
        " 문자열 하나)를 추가로 포함해 yaml 계약 블록 하나로만 응답하라."
    )

    def check(contract):
        if facts is not None:
            contracts.validate_result_against_facts(contract, facts)

    try:
        contract = request_contract(
            system_prompt,
            user_content,
            load_config(),
            "evaluate",
            extra_validator=check,
            metrics=tracker,
        )
    except (ContractParseError, ContractValidationError) as exc:
        return {
            **_needs_input("04_evaluation", "contract_validation", problems=str(exc)),
            "metrics": _merge_metrics(state, tracker),
        }

    final_text = mapper.unmask(contract.final_text)
    warnings = list(state.get("warnings") or [])
    mismatch = contracts.verify_byte_count(contract.byte_count, _neis_bytes(final_text))
    if mismatch:
        warnings.append(mismatch)

    return {
        "final_text": final_text,
        "output_path": output_path_rel,
        "lint_retry_count": 0,
        "warnings": warnings,
        "metrics": _merge_metrics(state, tracker),
    }


def _load_rules() -> dict[str, Any]:
    return linter.load_rules(ROOT / "rules.json")


def _neis_bytes(text: str) -> int:
    """모델이 주장한 byte_count를 믿지 않고 Linter와 같은 규칙으로 다시 센다."""
    return linter.neis_byte_count(text, _load_rules()["byte_count"])


def _policy_service():
    """규정 서비스. 코퍼스가 없거나 깨져도 파이프라인을 멈추지 않는다.

    규정 인용은 '있으면 더 좋은' 부가 정보다. 코퍼스를 못 읽는다고 세특 생성 자체가
    실패하면 안 되므로, 실패하면 None을 돌려주고 인용 없이 진행한다.
    """
    try:
        from .policy.service import get_service

        service = get_service()
        return None if service.is_empty else service
    except Exception:  # 코퍼스 설정 오류가 세특 생성을 막지 않게 한다
        return None


def _record_field_for(state: PipelineState) -> str:
    """현재 C버전은 교과 세특 흐름만 구현되어 있다."""
    return "subject_setuk"


def node_lint(state: PipelineState) -> dict[str, Any]:
    if _halted(state):
        return {}
    rules = _load_rules()
    result = linter.lint_text(state["final_text"], state["output_path"], rules)
    diagnostics = [asdict(item) for item in result.diagnostics]

    # --- 규정 근거 붙이기 (계획서: 최종 Linter 진단에 근거 규정과 페이지 표시) ---
    service = _policy_service()
    policy: dict[str, Any] = {}
    if service is not None:
        record_field = _record_field_for(state)
        findings = service.scan_text(state["final_text"], record_field=record_field)
        diagnostics = service.annotate_diagnostics(diagnostics, record_field=record_field)
        policy = {
            "policy_findings": [item.as_dict() for item in findings],
            "policy_index_version": service.index_version,
            "policy_retrieval_mode": service.retrieval_mode,
        }
        notice = service.check_policy_year(state.get("policy_year"))
        if notice:
            policy["warnings"] = [*(state.get("warnings") or []), notice]

    if result.passed:
        output_path = ROOT / state["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(state["final_text"], encoding="utf-8", newline="\n")
        return {
            "lint_passed": True,
            "lint_summary": linter.format_text(result),
            "lint_diagnostics": diagnostics,
            **policy,
        }
    return {
        "lint_passed": False,
        "lint_summary": linter.format_text(result),
        "lint_diagnostics": diagnostics,
        **policy,
    }


def route_after_lint(state: PipelineState) -> str:
    if _halted(state):
        return "end"
    if state.get("lint_passed"):
        return "end"
    if state.get("lint_retry_count", 0) >= MAX_LINT_RETRIES:
        return "end"
    return "fix"


def node_fix(state: PipelineState, config: RunnableConfig | None = None) -> dict[str, Any]:
    over = _budget_exceeded(state)
    if over:
        return {"error": over}
    mapper = _pseudonymizer(state, config)
    tracker = CallMetrics(stage="fix")
    system_prompt = load_prompt("04_evaluation.md")
    diagnostics = state.get("lint_diagnostics", [])

    # 규정 코퍼스가 block으로 분류한 항목은 Linter 진단과 함께 고치게 한다.
    # review 등급은 교사 판단 영역이므로 모델에게 넘기지 않는다.
    blocking = [
        item for item in (state.get("policy_findings") or []) if item.get("severity") == "block"
    ]
    policy_note = ""
    if blocking:
        policy_note = (
            "\n\n추가로, 학교생활기록부 기재요령상 기재할 수 없는 표현이 발견되었다. "
            "해당 표현을 근거 조항에 맞게 제거하거나 사실 장부 범위 안에서 다시 서술하라:\n"
            + json.dumps(blocking, ensure_ascii=False, indent=2)
        )

    user_content = (
        "다음은 방금 Linter를 통과하지 못한 최종본이다:\n\n"
        f"{mapper.mask(state['final_text'])}\n\n"
        f"Linter 진단 결과:\n{json.dumps(diagnostics, ensure_ascii=False, indent=2)}"
        f"{policy_note}\n\n"
        "AGENTS.md 5번의 수정 원칙(BYTE_LIMIT/FORBIDDEN_TERM/UNSUPPORTED_CHARACTER/"
        "TAB_CHARACTER/EMPTY_CONTENT)에 따라 진단된 오류만 근거로 최소 수정하라. 사실 장부에"
        " 없는 내용을 새로 추가하지 마라. EVALUATED_RESULT_V1 yaml 계약 블록에 `final_text`"
        " 필드(수정된 전체 본문)를 포함해 응답하라."
    )
    try:
        contract = request_contract(
            system_prompt, user_content, load_config(), "fix", metrics=tracker
        )
    except (ContractParseError, ContractValidationError) as exc:
        return {
            **_needs_input("04_evaluation(fix)", "contract_validation", problems=str(exc)),
            "metrics": _merge_metrics(state, tracker),
        }

    return {
        "final_text": mapper.unmask(contract.final_text),
        "lint_retry_count": state.get("lint_retry_count", 0) + 1,
        "metrics": _merge_metrics(state, tracker),
    }


def build_graph(checkpointer):
    graph = StateGraph(PipelineState)
    graph.add_node("ingest", node_ingest)
    graph.add_node("structure", node_structure)
    graph.add_node("draft", node_draft)
    graph.add_node("human_review", node_human_review)
    graph.add_node("evaluate", node_evaluate)
    graph.add_node("lint", node_lint)
    graph.add_node("fix", node_fix)

    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "structure")
    graph.add_edge("structure", "draft")
    graph.add_edge("draft", "human_review")
    graph.add_conditional_edges(
        "human_review", route_after_review, {"draft": "draft", "evaluate": "evaluate", "end": END}
    )
    graph.add_edge("evaluate", "lint")
    graph.add_conditional_edges("lint", route_after_lint, {"fix": "fix", "end": END})
    graph.add_edge("fix", "lint")

    return graph.compile(checkpointer=checkpointer)
