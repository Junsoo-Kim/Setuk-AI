"""MCP elicitation으로 교사 검토를 받는다.

## 왜 `submit_review`와 따로 두는가

`submit_review(run_id, decision)`은 **모델이 결정을 인자로 채워** 부르는 도구다. MCP
클라이언트는 대개 도구를 자동 승인하도록 설정할 수 있어서, 그렇게 되면 모델이 교사 대신
승인할 수 있다. 그래서 그 도구는 환경변수로 기본 차단해 두었다.

`request_teacher_review(run_id)`는 인자에 결정이 없다. 모델은 검토를 **요청**만 할 수 있고,
서버가 MCP `elicitation/create`로 클라이언트에게 "사람에게 이 질문을 보여 달라"고 한다.
승인·반려는 그 대화창에서 사람이 고른 값으로만 들어온다. 모델이 결정을 넘길 통로가
구조적으로 없으므로, 쓰기 도구인데도 별도 허용 없이 열 수 있다.

## 흐름

1. 클라이언트가 elicitation을 지원하지 않으면 거부한다(fail-closed). 웹 화면으로 안내한다.
2. 검토 대기 상태인 run의 초안과 해시를 읽는다.
3. 초안을 보여 주고 결정(승인/반려)과 반려 사유를 묻는다.
4. 답을 받은 뒤 run을 **다시** 읽어, 그 사이 상태나 초안이 바뀌었으면 기록하지 않는다
   (대화창이 열려 있는 동안 웹에서 먼저 처리했거나 초안이 재생성된 경우).
5. 웹 화면과 같은 `Review` 행과 감사 로그를 남기고 run을 `review_submitted`로 넘긴다.
   그래프 재개는 웹 서버(`db.claim_submitted_reviews`)가 한다 — 체크포인트·리스·결과 반영을
   가진 쪽이 실행을 맡고, MCP는 결정 채널만 맡는다.

## 신뢰 경계

elicitation 응답은 클라이언트가 보낸다. MCP 사양은 클라이언트가 이를 사용자에게 보여 주도록
요구하지만, 클라이언트 쪽 설정(예: Claude Code의 Elicitation hook)으로 응답을 자동으로 채울
수도 있다. 그 설정은 교사 본인의 도구에 대한 선택이므로 막을 수 없고, 대신 모든 결정에
채널(`mcp-elicitation`)과 클라이언트 이름을 남겨 웹 화면 승인과 구분되게 한다.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from .mcp_tools import ToolError

ACTOR = "mcp-elicitation"

# MCP elicitation은 평평한 원시 타입 필드만 받는다.
ELICITATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "title": "검토 결정",
            "enum": ["approve", "reject"],
            "enumNames": ["승인", "반려"],
        },
        "reason": {
            "type": "string",
            "title": "반려 사유 (반려할 때 필수)",
            "maxLength": 500,
        },
    },
    "required": ["decision"],
}

Elicit = Callable[[str, dict[str, Any]], Awaitable[Any]]


def _load_pending(database, run_id: str) -> dict[str, Any]:
    from .db import STATUS_AWAITING_REVIEW, Run

    with database.session() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise ToolError(f"알 수 없는 작업입니다: {run_id}")
        if run.status != STATUS_AWAITING_REVIEW:
            raise ToolError(f"지금은 검토할 수 있는 상태가 아닙니다: {run.status}")
        if not run.draft_text:
            raise ToolError("검토할 초안이 없습니다.")
        return {
            "pseudonym": run.pseudonym,
            "draft_text": run.draft_text,
            "draft_hash": run.draft_hash,
        }


def _message(run_id: str, pending: dict[str, Any]) -> str:
    return (
        f"[세특 초안 검토] 작업 {run_id} · 학생 {pending['pseudonym']}\n"
        f"초안 해시: {(pending['draft_hash'] or '')[:12]}\n\n"
        f"{pending['draft_text']}\n\n"
        "승인하면 평가·Linter 검사·저장 단계로 넘어갑니다. 반려하면 사유를 반영해 다시 씁니다. "
        "본문을 직접 고쳐서 승인하려면 웹 화면을 쓰세요."
    )


def _audit_only(database, run_id: str, action: str, detail: dict[str, Any]) -> None:
    from .db import Run, record_audit

    with database.session() as session:
        run = session.get(Run, run_id)
        record_audit(
            session,
            action=action,
            actor=ACTOR,
            run_id=run_id,
            pseudonym=run.pseudonym if run else None,
            detail=detail,
        )


def _record(
    database, run_id: str, expected_hash: str | None, decision: str, reason: str, client_name: str
) -> None:
    from sqlalchemy.exc import IntegrityError

    from .db import STATUS_AWAITING_REVIEW, STATUS_REVIEW_SUBMITTED, Review, Run, record_audit, utcnow

    try:
        with database.session() as session:
            run = session.get(Run, run_id)
            if run is None or run.status != STATUS_AWAITING_REVIEW:
                raise ToolError(
                    "검토하는 사이 작업 상태가 바뀌었습니다(웹 화면에서 먼저 처리했을 수 있습니다). "
                    "이 결정은 기록하지 않았습니다."
                )
            if run.draft_hash != expected_hash:
                raise ToolError(
                    "검토하는 사이 초안이 바뀌었습니다. 이 결정은 기록하지 않았습니다. "
                    "최신 초안으로 다시 검토를 요청하세요."
                )
            approved = decision == "approve"
            entry = Review(
                run_id=run_id,
                # 같은 초안에 대한 같은 결정이 두 번 들어와도 한 건만 남는다.
                idempotency_key=f"elicit-{decision}-{run.draft_hash}",
                decision=decision,
                reviewer=f"{ACTOR}:{client_name}"[:128],
                reason=reason or None,
                draft_hash=run.draft_hash,
                approved_hash=run.draft_hash if approved else None,
                edited=False,
                prompt_version=run.prompt_version,
                approved_at=utcnow() if approved else None,
            )
            session.add(entry)
            run.status = STATUS_REVIEW_SUBMITTED
            session.flush()
            record_audit(
                session,
                action=f"review.{decision}",
                actor=ACTOR,
                run_id=run_id,
                pseudonym=run.pseudonym,
                detail={
                    "channel": "mcp_elicitation",
                    "client": client_name,
                    "draft_hash": run.draft_hash,
                    "prompt_version": run.prompt_version,
                },
            )
    except IntegrityError as exc:
        raise ToolError("같은 초안에 대한 같은 결정이 이미 기록되어 있습니다.") from exc


async def request_teacher_review(
    run_id: str,
    *,
    database,
    elicit: Elicit,
    supports_elicitation: bool,
    client_name: str = "unknown",
) -> dict[str, Any]:
    if not supports_elicitation:
        raise ToolError(
            "이 MCP 클라이언트는 elicitation(사용자에게 직접 묻기)을 지원하지 않아 교사 검토를 "
            "받을 수 없습니다. 웹 화면에서 초안을 검토하세요."
        )

    pending = await asyncio.to_thread(_load_pending, database, run_id)
    answer = await elicit(_message(run_id, pending), ELICITATION_SCHEMA)
    action = getattr(answer, "action", None)

    base = {"run_id": run_id, "draft_hash": pending["draft_hash"]}
    if action != "accept":
        outcome = action if action in ("decline", "cancel") else "unknown"
        await asyncio.to_thread(
            _audit_only, database, run_id, f"review.elicitation_{outcome}", {"client": client_name}
        )
        return {
            **base,
            "recorded": False,
            "outcome": outcome,
            "note": "교사가 검토를 거절하거나 취소했습니다. 아무것도 기록하지 않았습니다.",
        }

    content = getattr(answer, "content", None) or {}
    decision = content.get("decision")
    reason = str(content.get("reason") or "").strip()
    if decision not in ("approve", "reject"):
        return {**base, "recorded": False, "outcome": "invalid", "note": "결정 값이 올바르지 않습니다."}
    if decision == "reject" and not reason:
        return {
            **base,
            "recorded": False,
            "outcome": "invalid",
            "note": "반려에는 사유가 필요합니다. 다시 검토를 요청하세요.",
        }

    await asyncio.to_thread(
        _record, database, run_id, pending["draft_hash"], decision, reason, client_name
    )
    return {
        **base,
        "decision": decision,
        "recorded": True,
        "outcome": "accept",
        "note": "교사의 결정을 기록했습니다. 웹 서버가 이어서 진행합니다(평가·Linter·저장).",
    }

