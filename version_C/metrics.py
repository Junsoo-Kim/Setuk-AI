"""Agent/HITL 지표 집계.

계획서 6장이 요구하는 지표를 DB에 이미 쌓인 원자료에서 계산한다. 별도 수집 파이프라인을
두지 않은 이유는, 필요한 원자료(runs·reviews·artifacts·audit_logs)가 이미 전부 남아 있고
규모가 작아서 조회 시점에 계산해도 충분하기 때문이다.

계산하는 것과 그 근거:

| 지표 | 계산 근거 |
| --- | --- |
| 작업 성공률 | `runs.status` 분포 |
| 교사 반려율 | `reviews.decision` 분포 |
| 초안 수정률 | `reviews.edited` |
| 초안 대비 교사 수정 거리 | `artifacts` draft vs approved의 정규화 편집 거리 |
| 초안→승인 소요 시간 | `reviews.approved_at - runs.created_at` |
| 평균 Linter 재시도 | `runs.lint_retry_count` |
| 단계별 실패율 | `audit_logs.detail.metrics[].parse_failures` |
| 복구 성공률 | `run.recovered` 이후 종료 상태 |
| 토큰·비용 | `audit_logs.detail.metrics[]`의 토큰 합 |

**표본이 적으면 지표는 거짓말을 한다.** 모든 비율에 분모를 함께 실어 보내고, 화면에서도
분모를 같이 보여 준다. "반려율 100%"가 2건 중 2건이라는 사실이 보이지 않으면 안 된다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import func, select

from .db import (
    STATUS_AWAITING_REVIEW,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_INTERRUPTED,
    STATUS_LINT_FAILED,
    STATUS_RUNNING,
    Artifact,
    AuditLog,
    Database,
    Review,
    Run,
    utcnow,
)

TERMINAL = (STATUS_DONE, STATUS_LINT_FAILED, STATUS_ERROR)


def _ratio(numerator: int, denominator: int) -> dict[str, Any]:
    """비율은 언제나 분모와 함께 보고한다. 표본이 3건인 100%는 100%가 아니다."""
    return {
        "value": round(numerator / denominator, 4) if denominator else None,
        "count": numerator,
        "of": denominator,
    }


def edit_distance(left: str, right: str) -> int:
    """문자 단위 Levenshtein 거리.

    세특 본문은 1,500바이트(한글 500자) 상한이라 O(n*m)으로 충분하다. 외부 의존성을
    더하지 않으려고 직접 구현했다.
    """
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    previous = list(range(len(right) + 1))
    for i, source_char in enumerate(left, start=1):
        current = [i]
        for j, target_char in enumerate(right, start=1):
            current.append(
                min(
                    previous[j] + 1,  # 삭제
                    current[j - 1] + 1,  # 삽입
                    previous[j - 1] + (source_char != target_char),  # 치환
                )
            )
        previous = current
    return previous[-1]


def normalized_edit_distance(left: str, right: str) -> float:
    """0.0(동일) ~ 1.0(완전히 다름). 길이가 다른 초안끼리 비교하기 위해 정규화한다."""
    longest = max(len(left), len(right))
    if not longest:
        return 0.0
    return round(edit_distance(left, right) / longest, 4)


def _aware(moment: datetime | None) -> datetime | None:
    """SQLite는 tz를 보존하지 않는다. 뺄셈 전에 UTC로 맞춘다."""
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 2)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 2)


def _metric_entries(logs: Iterable[AuditLog]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for log in logs:
        detail = log.detail or {}
        for item in detail.get("metrics") or []:
            if isinstance(item, dict):
                entries.append(item)
    return entries


def collect(db: Database, *, days: int | None = None) -> dict[str, Any]:
    """지표 한 묶음을 계산한다. `days`를 주면 그 기간의 작업만 본다."""
    since = utcnow() - timedelta(days=days) if days else None

    with db.session() as session:
        run_query = select(Run)
        if since is not None:
            run_query = run_query.where(Run.created_at >= since)
        runs = list(session.scalars(run_query).all())
        run_ids = {run.id for run in runs}

        reviews = [
            review
            for review in session.scalars(select(Review)).all()
            if not run_ids or review.run_id in run_ids
        ]
        audit_logs = [
            log
            for log in session.scalars(select(AuditLog)).all()
            if log.run_id is None or not run_ids or log.run_id in run_ids
        ]
        artifacts = [
            artifact
            for artifact in session.scalars(select(Artifact)).all()
            if artifact.run_id in run_ids
        ]

    status_counts: dict[str, int] = {}
    for run in runs:
        status_counts[run.status] = status_counts.get(run.status, 0) + 1

    finished = sum(status_counts.get(status, 0) for status in TERMINAL)
    succeeded = status_counts.get(STATUS_DONE, 0)

    # --- 교사 검토 ---
    approvals = [item for item in reviews if item.decision == "approve"]
    rejections = [item for item in reviews if item.decision == "reject"]
    edited = [item for item in approvals if item.edited]

    # --- 초안 대비 교사 수정 거리 ---
    by_run: dict[str, dict[str, str]] = {}
    for artifact in artifacts:
        by_run.setdefault(artifact.run_id, {})[artifact.kind] = artifact.content
    distances = [
        normalized_edit_distance(pair["draft"], pair["approved"])
        for pair in by_run.values()
        if pair.get("draft") and pair.get("approved")
    ]

    # --- 초안 생성부터 승인까지 ---
    created_at = {run.id: _aware(run.created_at) for run in runs}
    durations = []
    for review in approvals:
        start = created_at.get(review.run_id)
        approved = _aware(review.approved_at)
        if start and approved:
            durations.append((approved - start).total_seconds())

    # --- 단계별 계약 실패 ---
    entries = _metric_entries(audit_logs)
    stage_totals: dict[str, dict[str, int]] = {}
    input_tokens = output_tokens = 0
    for entry in entries:
        stage = str(entry.get("stage") or "unknown")
        bucket = stage_totals.setdefault(stage, {"calls": 0, "repairs": 0, "failures": 0})
        bucket["calls"] += int(entry.get("attempts") or 0)
        bucket["repairs"] += int(entry.get("repairs") or 0)
        bucket["failures"] += len(entry.get("parse_failures") or [])
        input_tokens += int(entry.get("input_tokens") or 0)
        output_tokens += int(entry.get("output_tokens") or 0)

    stage_failure_rate = {
        stage: _ratio(bucket["failures"], bucket["calls"])
        for stage, bucket in sorted(stage_totals.items())
    }

    # --- 재시작 복구 ---
    recovered_ids = {
        log.run_id for log in audit_logs if log.action == "run.recovered" and log.run_id
    }
    recovered_then_finished = sum(
        1 for run in runs if run.id in recovered_ids and run.status in TERMINAL
    )
    still_stuck = sum(
        1 for run in runs if run.id in recovered_ids and run.status == STATUS_INTERRUPTED
    )

    lint_retries = [run.lint_retry_count for run in runs if run.lint_retry_count is not None]

    return {
        "window_days": days,
        "generated_at": utcnow().isoformat(),
        "runs": {
            "total": len(runs),
            "by_status": status_counts,
            "in_flight": status_counts.get(STATUS_RUNNING, 0)
            + status_counts.get(STATUS_AWAITING_REVIEW, 0),
            "success_rate": _ratio(succeeded, finished),
        },
        "human_review": {
            "decisions": len(reviews),
            "rejection_rate": _ratio(len(rejections), len(reviews)),
            "edit_rate": _ratio(len(edited), len(approvals)),
            "median_edit_distance": _median(distances),
            "edit_distance_samples": len(distances),
            "median_seconds_to_approval": _median(durations),
            "approval_samples": len(durations),
        },
        "pipeline": {
            "mean_lint_retries": (
                round(sum(lint_retries) / len(lint_retries), 2) if lint_retries else None
            ),
            "stage_failure_rate": stage_failure_rate,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
        "recovery": {
            "recovered_runs": len(recovered_ids),
            "resolved_after_recovery": _ratio(recovered_then_finished, len(recovered_ids)),
            "still_interrupted": still_stuck,
        },
    }
