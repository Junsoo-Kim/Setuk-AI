"""보존 기간과 학생별 데이터 삭제.

교육 데이터는 목적을 다한 뒤에도 남아 있으면 그 자체가 위험이다. 이 프로젝트가
저장하는 것 중 학생을 식별할 수 있는 것은 세 가지다.

| 저장 위치 | 내용 | 삭제 대상 |
| --- | --- | --- |
| `runs.draft_text` | AI 초안 원문 | 예 |
| `artifacts.content` | 사실 장부·초안·승인본·최종본 | 예 |
| `audit_logs` | 누가 언제 무엇을 했는지(가명만) | 아니오(기본) |

감사 로그를 기본 보존 대상에서 뺀 이유: 감사 로그는 **삭제 사실 자체를 증명하는**
기록이다. 초안과 함께 지워 버리면 "언제 무엇을 지웠는지"를 확인할 수 없다. 로그에는
가명(`stu_xxxxxxxx`)과 해시만 있고 본문이나 실명이 없으므로 남겨 두는 편이 안전하다.
정말 전부 지워야 한다면 `purge_student(..., include_audit=True)`를 쓴다.

`세특/<식별자>.md` 같은 **결과 파일은 지우지 않는다.** 그건 교사가 NEIS에 옮겨 적을
산출물이지 이 시스템의 부산물이 아니다. 무엇을 지우고 무엇을 남기는지는 교사가
알 수 있어야 하므로, 삭제 함수는 지운 항목 수를 항상 돌려준다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select, update

from .db import Artifact, AuditLog, Database, Review, Run, record_audit, utcnow

# 0이면 자동 삭제를 하지 않는다. 학교마다 보존 정책이 다르므로 기본값을 강하게 잡지 않는다.
DEFAULT_RETENTION_DAYS = 0


@dataclass(frozen=True)
class RetentionPolicy:
    """무엇을 얼마나 오래 두는지."""

    draft_days: int = DEFAULT_RETENTION_DAYS
    artifact_days: int = DEFAULT_RETENTION_DAYS
    audit_days: int = 0  # 0 = 무기한. 감사 로그는 삭제 사실의 증거다.

    @property
    def enabled(self) -> bool:
        return bool(self.draft_days or self.artifact_days or self.audit_days)

    def describe(self) -> dict[str, Any]:
        def label(days: int) -> str:
            return f"{days}일" if days else "무기한"

        return {
            "draft_text": label(self.draft_days),
            "artifacts": label(self.artifact_days),
            "audit_logs": label(self.audit_days),
            "enabled": self.enabled,
        }


def load_policy() -> RetentionPolicy:
    """`.env`에서 보존 기간을 읽는다. 미설정이면 자동 삭제를 하지 않는다."""

    def read(name: str, fallback: int) -> int:
        raw = os.environ.get(name, "").strip()
        if not raw.lstrip("-").isdigit():
            return fallback
        return max(0, int(raw))

    days = read("SETUK_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)
    return RetentionPolicy(
        draft_days=read("SETUK_RETENTION_DRAFT_DAYS", days),
        artifact_days=read("SETUK_RETENTION_ARTIFACT_DAYS", days),
        audit_days=read("SETUK_RETENTION_AUDIT_DAYS", 0),
    )


def _cutoff(days: int) -> datetime | None:
    return utcnow() - timedelta(days=days) if days else None


def purge_expired(db: Database, policy: RetentionPolicy | None = None) -> dict[str, int]:
    """보존 기간이 지난 데이터를 지운다. 지운 개수를 돌려준다.

    초안은 행 자체가 아니라 **본문만** 비운다. run 행이 사라지면 "그런 작업이 있었다"는
    사실까지 사라져 감사 로그의 run_id가 미아가 되기 때문이다.
    """
    active = policy or load_policy()
    removed = {"draft_text": 0, "artifacts": 0, "audit_logs": 0}
    if not active.enabled:
        return removed

    with db.session() as session:
        draft_cutoff = _cutoff(active.draft_days)
        if draft_cutoff is not None:
            result = session.execute(
                update(Run)
                .where(Run.created_at < draft_cutoff, Run.draft_text.is_not(None))
                .values(draft_text=None)
                .execution_options(synchronize_session=False)
            )
            removed["draft_text"] = result.rowcount or 0

        artifact_cutoff = _cutoff(active.artifact_days)
        if artifact_cutoff is not None:
            result = session.execute(
                delete(Artifact)
                .where(Artifact.created_at < artifact_cutoff)
                .execution_options(synchronize_session=False)
            )
            removed["artifacts"] = result.rowcount or 0

        audit_cutoff = _cutoff(active.audit_days)
        if audit_cutoff is not None:
            result = session.execute(
                delete(AuditLog)
                .where(AuditLog.created_at < audit_cutoff)
                .execution_options(synchronize_session=False)
            )
            removed["audit_logs"] = result.rowcount or 0

        if any(removed.values()):
            record_audit(
                session,
                action="retention.purged",
                detail={**removed, "policy": active.describe()},
            )
    return removed


def purge_student(
    db: Database,
    *,
    student_key: str | None = None,
    pseudonym: str | None = None,
    include_audit: bool = False,
    actor: str = "teacher",
) -> dict[str, int]:
    """한 학생의 데이터를 지운다(학생별 데이터 삭제 API).

    `student_key`(실명)나 `pseudonym` 중 하나로 지정한다. 화면·API에서는 가명을 쓰는 편이
    안전하다 — 삭제 요청 로그에 실명을 남기지 않아도 되기 때문이다.

    지우는 것: 초안 본문, 산출물, 검토 이력, run 행.
    남기는 것: 감사 로그(삭제했다는 사실의 증거), `세특/*.md` 결과 파일.
    """
    if not student_key and not pseudonym:
        raise ValueError("student_key 또는 pseudonym 중 하나는 있어야 합니다.")

    from . import privacy

    target = pseudonym or privacy.pseudonym_for(student_key or "")
    removed = {"runs": 0, "artifacts": 0, "reviews": 0, "audit_logs": 0}

    with db.session() as session:
        run_ids = list(
            session.scalars(select(Run.id).where(Run.pseudonym == target)).all()
        )
        if run_ids:
            removed["artifacts"] = (
                session.execute(
                    delete(Artifact)
                    .where(Artifact.run_id.in_(run_ids))
                    .execution_options(synchronize_session=False)
                ).rowcount
                or 0
            )
            removed["reviews"] = (
                session.execute(
                    delete(Review)
                    .where(Review.run_id.in_(run_ids))
                    .execution_options(synchronize_session=False)
                ).rowcount
                or 0
            )
            removed["runs"] = (
                session.execute(
                    delete(Run)
                    .where(Run.id.in_(run_ids))
                    .execution_options(synchronize_session=False)
                ).rowcount
                or 0
            )

        if include_audit:
            removed["audit_logs"] = (
                session.execute(
                    delete(AuditLog)
                    .where(AuditLog.pseudonym == target)
                    .execution_options(synchronize_session=False)
                ).rowcount
                or 0
            )

        # 삭제 자체는 반드시 기록한다. 가명만 남기므로 이 기록이 실명을 되살리지 않는다.
        record_audit(
            session,
            action="retention.student_deleted",
            actor=actor,
            pseudonym=target,
            detail={**removed, "include_audit": include_audit},
        )
    return removed
