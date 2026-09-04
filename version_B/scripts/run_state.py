from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

ALLOWED_MODES = ("A", "B", "C", "E")

STATUSES = (
    "INITIALIZED",
    "REPORT_INGESTED",
    "STRUCTURED",
    "DRAFTED",
    "AWAITING_REVIEW",
    "REVIEWED",
    "REVIEW_REJECTED",
    "EVALUATED",
    "LINTED_PASS",
    "LINTED_FAIL",
)


class RunStateError(Exception):
    pass


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def state_path(run_state_dir: Path, student_key: str) -> Path:
    if not student_key or "/" in student_key or "\\" in student_key:
        raise RunStateError(f"student_key에 경로 구분자를 쓸 수 없습니다: {student_key}")
    return run_state_dir / f"{student_key}.json"


def review_draft_path(run_state_dir: Path, student_key: str) -> Path:
    return run_state_dir / f"{student_key}_review.md"


def new_state(student_key: str, mode: str) -> dict[str, Any]:
    if mode not in ALLOWED_MODES:
        raise RunStateError(
            f"run_state는 {ALLOWED_MODES} 모드에서만 사용합니다. 입력값: {mode}"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "student_key": student_key,
        "mode": mode,
        "status": "INITIALIZED",
        "updated_at": _now_iso(),
        "steps": {
            "report_ingestion": {"done": False, "yaml_path": None},
            "data_structuring": {"done": False},
            "drafting": {"done": False, "draft_text": None, "draft_saved_path": None},
            "review": {
                "done": False,
                "approved": None,
                "edited_text": None,
                "reviewer_note": None,
            },
            "evaluation": {"done": False, "output_path": None},
            "lint": {"done": False, "pass": None, "retry_count": 0, "summary": None},
        },
    }


def validate_state(state: dict[str, Any]) -> None:
    if not isinstance(state, dict):
        raise RunStateError("run_state 최상위 값은 객체여야 합니다.")
    if state.get("schema_version") != SCHEMA_VERSION:
        raise RunStateError("지원하지 않는 run_state schema_version입니다.")
    if not isinstance(state.get("student_key"), str) or not state["student_key"]:
        raise RunStateError("student_key가 비어 있습니다.")
    if state.get("mode") not in ALLOWED_MODES:
        raise RunStateError(f"mode는 {ALLOWED_MODES} 중 하나여야 합니다.")
    if state.get("status") not in STATUSES:
        raise RunStateError(f"알 수 없는 status입니다: {state.get('status')!r}")
    steps = state.get("steps")
    if not isinstance(steps, dict):
        raise RunStateError("steps가 없습니다.")
    for key in (
        "report_ingestion",
        "data_structuring",
        "drafting",
        "review",
        "evaluation",
        "lint",
    ):
        if key not in steps or not isinstance(steps[key], dict):
            raise RunStateError(f"steps.{key}가 없습니다.")


def load_state(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise RunStateError(f"run_state 파일을 읽을 수 없습니다: {path} ({exc})") from exc
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RunStateError(f"run_state JSON 문법이 잘못되었습니다: {path}:{exc.lineno}:{exc.colno}") from exc
    validate_state(state)
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    validate_state(state)
    state = {**state, "updated_at": _now_iso()}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    tmp_path.replace(path)


def require_status(state: dict[str, Any], allowed: tuple[str, ...], action: str) -> None:
    if state["status"] not in allowed:
        raise RunStateError(
            f"{action}은(는) status가 {allowed}일 때만 가능합니다. 현재 status: {state['status']}"
        )


def set_report_ingested(state: dict[str, Any], yaml_path: str) -> dict[str, Any]:
    require_status(state, ("INITIALIZED",), "report_ingestion 기록")
    state = json.loads(json.dumps(state))
    state["steps"]["report_ingestion"] = {"done": True, "yaml_path": yaml_path}
    state["status"] = "REPORT_INGESTED"
    return state


def set_structured(state: dict[str, Any]) -> dict[str, Any]:
    require_status(state, ("REPORT_INGESTED",), "data_structuring 기록")
    state = json.loads(json.dumps(state))
    state["steps"]["data_structuring"] = {"done": True}
    state["status"] = "STRUCTURED"
    return state


def set_drafted(state: dict[str, Any], draft_text: str) -> dict[str, Any]:
    require_status(state, ("STRUCTURED", "REVIEW_REJECTED"), "drafting 기록")
    state = json.loads(json.dumps(state))
    state["steps"]["drafting"] = {
        "done": True,
        "draft_text": draft_text,
        "draft_saved_path": None,
    }
    state["steps"]["review"] = {
        "done": False,
        "approved": None,
        "edited_text": None,
        "reviewer_note": None,
    }
    state["status"] = "DRAFTED"
    return state


def set_awaiting_review(state: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
    require_status(state, ("DRAFTED",), "awaiting_review 전환")
    state = json.loads(json.dumps(state))
    state["status"] = "AWAITING_REVIEW"
    if reason:
        state["steps"]["review"]["reviewer_note"] = reason
    return state


def mark_reviewed(
    state: dict[str, Any], *, approved: bool, edited_text: str, reviewer_note: str | None
) -> dict[str, Any]:
    require_status(state, ("AWAITING_REVIEW",), "리뷰 승인/반려 기록")
    state = json.loads(json.dumps(state))
    state["steps"]["review"] = {
        "done": True,
        "approved": approved,
        "edited_text": edited_text,
        "reviewer_note": reviewer_note,
    }
    state["status"] = "REVIEWED" if approved else "REVIEW_REJECTED"
    return state


def set_evaluated(state: dict[str, Any], output_path: str) -> dict[str, Any]:
    require_status(state, ("REVIEWED",), "evaluation 기록")
    state = json.loads(json.dumps(state))
    state["steps"]["evaluation"] = {"done": True, "output_path": output_path}
    state["status"] = "EVALUATED"
    return state


def set_linted(
    state: dict[str, Any], *, passed: bool, retry_count: int, summary: str | None
) -> dict[str, Any]:
    require_status(state, ("EVALUATED", "LINTED_FAIL"), "lint 결과 기록")
    if retry_count < 0:
        raise RunStateError("retry_count는 0 이상이어야 합니다.")
    state = json.loads(json.dumps(state))
    state["steps"]["lint"] = {
        "done": True,
        "pass": passed,
        "retry_count": retry_count,
        "summary": summary,
    }
    state["status"] = "LINTED_PASS" if passed else "LINTED_FAIL"
    return state


def format_status_line(state: dict[str, Any]) -> str:
    return (
        f"{state['student_key']} [{state['mode']}] status={state['status']} "
        f"updated_at={state['updated_at']}"
    )


def _cmd_init(args: argparse.Namespace) -> int:
    path = state_path(args.dir, args.student_key)
    if path.exists() and not args.force:
        raise RunStateError(f"이미 존재합니다(덮어쓰려면 --force): {path}")
    save_state(path, new_state(args.student_key, args.mode))
    print(f"생성됨: {path}")
    return 0


def _load(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    path = state_path(args.dir, args.student_key)
    return path, load_state(path)


def _cmd_set_report_ingested(args: argparse.Namespace) -> int:
    path, state = _load(args)
    save_state(path, set_report_ingested(state, args.yaml_path))
    print(f"REPORT_INGESTED: {path}")
    return 0


def _cmd_set_structured(args: argparse.Namespace) -> int:
    path, state = _load(args)
    save_state(path, set_structured(state))
    print(f"STRUCTURED: {path}")
    return 0


def _cmd_set_drafted(args: argparse.Namespace) -> int:
    path, state = _load(args)
    draft_text = args.draft_file.read_text(encoding="utf-8-sig")
    save_state(path, set_drafted(state, draft_text))
    print(f"DRAFTED: {path}")
    return 0


def _cmd_set_awaiting_review(args: argparse.Namespace) -> int:
    path, state = _load(args)
    save_state(path, set_awaiting_review(state, args.reason))
    print(f"AWAITING_REVIEW: {path}")
    print("교사 검토가 필요합니다. 다음 명령으로 검토 화면을 여세요:")
    print(f"  & '.\\python_portable\\python.exe' '.\\scripts\\review_cli.py' 'open' '{args.student_key}'")
    return 0


def _cmd_set_evaluated(args: argparse.Namespace) -> int:
    path, state = _load(args)
    save_state(path, set_evaluated(state, args.output_path))
    print(f"EVALUATED: {path}")
    return 0


def _cmd_set_linted(args: argparse.Namespace) -> int:
    path, state = _load(args)
    save_state(
        path,
        set_linted(state, passed=(args.result == "pass"), retry_count=args.retry_count, summary=args.summary),
    )
    print(f"LINTED_{args.result.upper()}: {path}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    path, state = _load(args)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    if not args.dir.exists():
        print("(run_state 폴더가 아직 없습니다)")
        return 0
    found = False
    for path in sorted(args.dir.glob("*.json")):
        try:
            state = load_state(path)
        except RunStateError as exc:
            print(f"[손상됨] {path.name}: {exc}")
            continue
        found = True
        print(format_status_line(state))
    if not found:
        print("(대기 중인 run_state가 없습니다)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="run_state/<식별자>.json 체크포인트 관리")
    parser.add_argument("--dir", type=Path, default=Path("run_state"), help="run_state 폴더 (기본: ./run_state)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="새 학생 체크포인트 생성")
    p_init.add_argument("student_key")
    p_init.add_argument("mode", choices=ALLOWED_MODES)
    p_init.add_argument("--force", action="store_true", help="기존 파일 덮어쓰기")
    p_init.set_defaults(func=_cmd_init)

    p_ri = sub.add_parser("set-report-ingested", help="01단계 완료 기록")
    p_ri.add_argument("student_key")
    p_ri.add_argument("--yaml-path", required=True)
    p_ri.set_defaults(func=_cmd_set_report_ingested)

    p_st = sub.add_parser("set-structured", help="02단계 완료 기록")
    p_st.add_argument("student_key")
    p_st.set_defaults(func=_cmd_set_structured)

    p_dr = sub.add_parser("set-drafted", help="03단계 완료 기록(초안 텍스트 파일 필요)")
    p_dr.add_argument("student_key")
    p_dr.add_argument("--draft-file", required=True, type=Path)
    p_dr.set_defaults(func=_cmd_set_drafted)

    p_aw = sub.add_parser("set-awaiting-review", help="교사 검토 대기로 전환(인터럽트 지점)")
    p_aw.add_argument("student_key")
    p_aw.add_argument("--reason")
    p_aw.set_defaults(func=_cmd_set_awaiting_review)

    p_ev = sub.add_parser("set-evaluated", help="04단계 완료 기록")
    p_ev.add_argument("student_key")
    p_ev.add_argument("--output-path", required=True)
    p_ev.set_defaults(func=_cmd_set_evaluated)

    p_li = sub.add_parser("set-linted", help="Linter 실행 결과 기록")
    p_li.add_argument("student_key")
    p_li.add_argument("--result", required=True, choices=("pass", "fail"))
    p_li.add_argument("--retry-count", required=True, type=int)
    p_li.add_argument("--summary")
    p_li.set_defaults(func=_cmd_set_linted)

    p_sh = sub.add_parser("show", help="특정 학생의 run_state JSON 원문 출력")
    p_sh.add_argument("student_key")
    p_sh.set_defaults(func=_cmd_show)

    p_ls = sub.add_parser("list", help="run_state 폴더의 모든 학생 상태 요약 출력")
    p_ls.set_defaults(func=_cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RunStateError as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
