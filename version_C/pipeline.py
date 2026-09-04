from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .bridge import ROOT, extract_docx, linter
from .llm import ContractParseError, call_agent, contract_as_text, load_config, parse_contract
from .prompts import load_prompt

MAX_LINT_RETRIES = 5


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
    mode: str
    report_path: str
    yaml_path: str
    yaml_content: str
    structured_facts_yaml: str
    draft_text: str
    review_reason: str | None
    reviewed_text: str
    final_text: str
    output_path: str
    lint_retry_count: int
    lint_passed: bool
    lint_summary: str
    lint_diagnostics: list[dict[str, Any]]
    error: str | None


def node_ingest(state: PipelineState) -> dict[str, Any]:
    if state.get("error"):
        return {}

    try:
        _sanitize_student_key(state["student_key"])
    except PathSafetyError as exc:
        return {"error": str(exc)}

    if state["mode"] == "yaml":
        try:
            yaml_path = _resolve_within(state.get("yaml_path"), "학생정보", ".yaml")
        except PathSafetyError as exc:
            return {"error": str(exc)}
        if not yaml_path.is_file():
            return {"error": f"YAML 파일을 찾을 수 없습니다: {yaml_path}"}
        return {
            "yaml_path": state["yaml_path"],
            "yaml_content": yaml_path.read_text(encoding="utf-8-sig"),
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

    system_prompt = load_prompt("01_report_ingestion.md")
    user_content = (
        f"학생명: {state['student_key']}\n"
        f"보고서 경로: {report_path_rel}\n\n"
        "다음은 scripts/extract_docx.py로 추출한 보고서 본문이다(문단 순서 보존):\n\n"
        f"{extracted['text']}\n\n"
        "위 지침을 그대로 따라 학생 YAML을 만들어라. 이 코드가 파일 저장을 대신 하므로, "
        "REPORT_INGESTED_V1 계약에 지침이 정의한 필드 외에 `yaml_body` 필드(저장할 YAML 파일"
        " 전체 내용, 문자열 하나)를 추가로 포함해 yaml 계약 블록 하나로만 응답하라. "
        "추가 정보가 필요하면 REPORT_NEEDS_INPUT_V1로 응답하라."
    )
    raw = call_agent(system_prompt, user_content, load_config())
    try:
        contract = parse_contract(raw)
    except ContractParseError as exc:
        return {"error": f"01단계 응답 파싱 실패: {exc}"}

    if contract.get("contract") == "REPORT_NEEDS_INPUT_V1":
        return {"error": f"보고서 입력에 추가 정보가 필요합니다: {contract.get('questions')}"}

    yaml_path_rel = contract.get("yaml_path")
    yaml_body = contract.get("yaml_body")
    if not yaml_path_rel or not yaml_body:
        return {"error": "01단계 응답에 yaml_path 또는 yaml_body가 없습니다."}

    try:
        saved_path = _resolve_within(yaml_path_rel, "학생정보", ".yaml")
    except PathSafetyError as exc:
        return {"error": f"01단계가 반환한 yaml_path가 안전하지 않습니다: {exc}"}

    saved_path.parent.mkdir(parents=True, exist_ok=True)
    saved_path.write_text(yaml_body, encoding="utf-8", newline="\n")
    return {"yaml_path": yaml_path_rel, "yaml_content": yaml_body}


def node_structure(state: PipelineState) -> dict[str, Any]:
    if state.get("error"):
        return {}
    system_prompt = load_prompt("02_data_structuring.md")
    user_content = (
        "다음은 입력 학생 YAML의 전체 내용이다(별도로 파일을 다시 읽지 말고 이 내용을 그대로"
        " 입력으로 사용하라):\n\n"
        f"```yaml\n{state['yaml_content']}\n```\n\n"
        "위 지침을 그대로 따라 STRUCTURED_FACTS_V1 또는 NEEDS_INPUT_V1 yaml 계약 블록 하나로만"
        " 응답하라."
    )
    raw = call_agent(system_prompt, user_content, load_config())
    try:
        contract = parse_contract(raw)
    except ContractParseError as exc:
        return {"error": f"02단계 응답 파싱 실패: {exc}"}
    if contract.get("contract") == "NEEDS_INPUT_V1":
        return {"error": f"구조화에 추가 정보가 필요합니다: {contract.get('questions')}"}
    return {"structured_facts_yaml": contract_as_text(contract)}


def node_draft(state: PipelineState) -> dict[str, Any]:
    if state.get("error"):
        return {}
    system_prompt = load_prompt("03_drafting.md")
    reason = state.get("review_reason")
    revision_note = (
        f"\n\n이전 교사 검토에서 반려되었다. 다음 사유를 반영해 다시 작성하라: {reason}"
        if reason
        else ""
    )
    user_content = (
        "다음은 직전 단계의 STRUCTURED_FACTS_V1이다:\n\n"
        f"```yaml\n{state['structured_facts_yaml']}```\n"
        f"{revision_note}\n\n"
        "위 지침을 그대로 따라 DRAFT_V1 yaml 계약 블록 하나로만 응답하라."
    )
    raw = call_agent(system_prompt, user_content, load_config())
    try:
        contract = parse_contract(raw)
    except ContractParseError as exc:
        return {"error": f"03단계 응답 파싱 실패: {exc}"}
    draft_text = contract.get("text")
    if not draft_text:
        return {"error": "03단계 응답에 text 필드가 없습니다."}
    return {"draft_text": draft_text, "review_reason": None}


def node_human_review(state: PipelineState) -> dict[str, Any]:
    if state.get("error"):
        return {}
    decision = interrupt(
        {
            "type": "draft_review",
            "student_key": state["student_key"],
            "draft_text": state["draft_text"],
        }
    )
    if decision.get("approved"):
        edited = (decision.get("edited_text") or "").strip()
        return {"reviewed_text": edited or state["draft_text"], "review_reason": None}
    reason = (decision.get("reason") or "").strip() or "사유 없음"
    return {"reviewed_text": None, "review_reason": reason}


def route_after_review(state: PipelineState) -> str:
    if state.get("error"):
        return "end"
    if state.get("reviewed_text"):
        return "evaluate"
    return "draft"


def node_evaluate(state: PipelineState) -> dict[str, Any]:
    if state.get("error"):
        return {}
    try:
        student_key = _sanitize_student_key(state["student_key"])
    except PathSafetyError as exc:
        return {"error": str(exc)}
    output_path_rel = f"세특/{student_key}.md"
    system_prompt = load_prompt("04_evaluation.md")
    user_content = (
        "다음은 STRUCTURED_FACTS_V1이다:\n\n"
        f"```yaml\n{state['structured_facts_yaml']}```\n\n"
        "다음은 교사가 검토·승인한 DRAFT_V1.text이다(이 텍스트가 이번 평가·수정의 대상이다):\n\n"
        f"{state['reviewed_text']}\n\n"
        f"output_path는 '{output_path_rel}'로 고정한다.\n\n"
        "위 지침을 그대로 따라 평가·수정하라. 이 코드가 파일 저장을 대신 하므로, "
        "EVALUATED_RESULT_V1 계약에 지침이 정의한 필드 외에 `final_text` 필드(저장할 전체 본문,"
        " 문자열 하나)를 추가로 포함해 yaml 계약 블록 하나로만 응답하라."
    )
    raw = call_agent(system_prompt, user_content, load_config())
    try:
        contract = parse_contract(raw)
    except ContractParseError as exc:
        return {"error": f"04단계 응답 파싱 실패: {exc}"}
    final_text = contract.get("final_text")
    if not final_text:
        return {"error": "04단계 응답에 final_text 필드가 없습니다."}
    return {"final_text": final_text, "output_path": output_path_rel, "lint_retry_count": 0}


def node_lint(state: PipelineState) -> dict[str, Any]:
    if state.get("error"):
        return {}
    rules = linter.load_rules(ROOT / "rules.json")
    result = linter.lint_text(state["final_text"], state["output_path"], rules)
    if result.passed:
        output_path = ROOT / state["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(state["final_text"], encoding="utf-8", newline="\n")
        return {"lint_passed": True, "lint_summary": linter.format_text(result)}
    return {
        "lint_passed": False,
        "lint_summary": linter.format_text(result),
        "lint_diagnostics": [asdict(item) for item in result.diagnostics],
    }


def route_after_lint(state: PipelineState) -> str:
    if state.get("error"):
        return "end"
    if state.get("lint_passed"):
        return "end"
    if state.get("lint_retry_count", 0) >= MAX_LINT_RETRIES:
        return "end"
    return "fix"


def node_fix(state: PipelineState) -> dict[str, Any]:
    system_prompt = load_prompt("04_evaluation.md")
    diagnostics = state.get("lint_diagnostics", [])
    user_content = (
        "다음은 방금 Linter를 통과하지 못한 최종본이다:\n\n"
        f"{state['final_text']}\n\n"
        f"Linter 진단 결과:\n{json.dumps(diagnostics, ensure_ascii=False, indent=2)}\n\n"
        "AGENTS.md 5번의 수정 원칙(BYTE_LIMIT/FORBIDDEN_TERM/UNSUPPORTED_CHARACTER/"
        "TAB_CHARACTER/EMPTY_CONTENT)에 따라 진단된 오류만 근거로 최소 수정하라. 사실 장부에"
        " 없는 내용을 새로 추가하지 마라. EVALUATED_RESULT_V1 yaml 계약 블록에 `final_text`"
        " 필드(수정된 전체 본문)를 포함해 응답하라."
    )
    raw = call_agent(system_prompt, user_content, load_config())
    try:
        contract = parse_contract(raw)
    except ContractParseError as exc:
        return {"error": f"수정 응답 파싱 실패: {exc}"}
    final_text = contract.get("final_text")
    if not final_text:
        return {"error": "수정 응답에 final_text 필드가 없습니다."}
    return {"final_text": final_text, "lint_retry_count": state.get("lint_retry_count", 0) + 1}


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
