from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_state as rs


def cmd_open(run_state_dir: Path, student_key: str) -> str:
    path = rs.state_path(run_state_dir, student_key)
    state = rs.load_state(path)
    rs.require_status(state, ("AWAITING_REVIEW",), "초안 검토 화면 열기")

    draft_text = state["steps"]["drafting"]["draft_text"] or ""
    review_path = rs.review_draft_path(run_state_dir, student_key)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(draft_text, encoding="utf-8", newline="\n")

    return (
        f"검토용 초안을 저장했습니다: {review_path}\n"
        "이 파일을 열어 필요하면 직접 고친 뒤 저장하세요. 그대로 승인해도 됩니다.\n"
        "검토가 끝나면 다음 중 하나를 실행하세요.\n"
        f"  승인: & '.\\python_portable\\python.exe' '.\\scripts\\review_cli.py' 'approve' '{student_key}'\n"
        f"  반려: & '.\\python_portable\\python.exe' '.\\scripts\\review_cli.py' 'reject' '{student_key}' '--reason' '<이유>'"
    )


def cmd_approve(run_state_dir: Path, student_key: str, note: str | None) -> str:
    path = rs.state_path(run_state_dir, student_key)
    state = rs.load_state(path)
    rs.require_status(state, ("AWAITING_REVIEW",), "초안 승인")

    review_path = rs.review_draft_path(run_state_dir, student_key)
    if not review_path.exists():
        raise rs.RunStateError(
            f"검토용 초안 파일이 없습니다. 먼저 'open'을 실행하세요: {review_path}"
        )
    edited_text = review_path.read_text(encoding="utf-8-sig")
    if not edited_text.strip():
        raise rs.RunStateError("검토용 초안 파일이 비어 있습니다. 승인하기 전에 내용을 확인하세요.")

    new = rs.mark_reviewed(state, approved=True, edited_text=edited_text, reviewer_note=note)
    rs.save_state(path, new)
    review_path.unlink(missing_ok=True)

    return (
        f"승인됨(REVIEWED): {path}\n"
        "이제 AI에게 다음 단계(04_evaluation)를 이어서 진행해 달라고 요청하세요."
    )


def cmd_reject(run_state_dir: Path, student_key: str, reason: str) -> str:
    path = rs.state_path(run_state_dir, student_key)
    state = rs.load_state(path)
    rs.require_status(state, ("AWAITING_REVIEW",), "초안 반려")

    review_path = rs.review_draft_path(run_state_dir, student_key)
    edited_text = None
    if review_path.exists():
        edited_text = review_path.read_text(encoding="utf-8-sig")

    new = rs.mark_reviewed(state, approved=False, edited_text=edited_text, reviewer_note=reason)
    rs.save_state(path, new)
    review_path.unlink(missing_ok=True)

    return (
        f"반려됨(REVIEW_REJECTED): {path}\n"
        f"반려 사유: {reason}\n"
        "AI에게 이 사유를 반영해 초안을 다시 작성해 달라고 요청하세요."
    )


def cmd_status(run_state_dir: Path, student_key: str | None) -> str:
    if student_key:
        path = rs.state_path(run_state_dir, student_key)
        state = rs.load_state(path)
        return rs.format_status_line(state)

    if not run_state_dir.exists():
        return "(run_state 폴더가 아직 없습니다)"
    lines = []
    for candidate in sorted(run_state_dir.glob("*.json")):
        try:
            state = rs.load_state(candidate)
        except rs.RunStateError as exc:
            lines.append(f"[손상됨] {candidate.name}: {exc}")
            continue
        lines.append(rs.format_status_line(state))
    return "\n".join(lines) if lines else "(대기 중인 run_state가 없습니다)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="세특/창체 초안 검토 로컬 CLI")
    parser.add_argument("--dir", type=Path, default=Path("run_state"), help="run_state 폴더 (기본: ./run_state)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_open = sub.add_parser("open", help="검토용 초안 파일을 연다")
    p_open.add_argument("student_key")

    p_approve = sub.add_parser("approve", help="검토를 승인한다")
    p_approve.add_argument("student_key")
    p_approve.add_argument("--note")

    p_reject = sub.add_parser("reject", help="검토를 반려한다")
    p_reject.add_argument("student_key")
    p_reject.add_argument("--reason", required=True)

    p_status = sub.add_parser("status", help="run_state 상태를 조회한다")
    p_status.add_argument("student_key", nargs="?")

    return parser


def main(argv: list[str] | None = None) -> int:
    rs.configure_stdio()
    args = build_parser().parse_args(argv)
    try:
        if args.command == "open":
            print(cmd_open(args.dir, args.student_key))
        elif args.command == "approve":
            print(cmd_approve(args.dir, args.student_key, args.note))
        elif args.command == "reject":
            print(cmd_reject(args.dir, args.student_key, args.reason))
        elif args.command == "status":
            print(cmd_status(args.dir, args.student_key))
    except rs.RunStateError as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
