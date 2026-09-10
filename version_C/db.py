"""작업 목록·검토 이력·감사 로그의 영속 계층.

v1은 작업 목록을 프로세스 메모리(`server._RUNS`)에 뒀다. LangGraph 체크포인트는
SQLite에 남아 있어도 서버를 재시작하면 UI가 thread_id를 모르므로 재개가 사실상
불가능했다. 여기서는 run 메타데이터와 체크포인트가 **같은 식별자(run.id ==
thread_id)로 같은 데이터베이스**를 가리키게 한다.

`DATABASE_URL`로 백엔드를 고른다.

- 미설정: `sqlite:///<version_C>/setuk_c.sqlite3` (혼자 쓰는 로컬 기본값)
- `postgresql+psycopg://...`: 운영. LangGraph 체크포인터도 PostgresSaver로 함께 바뀐다.

개인정보 원칙: `runs.student_key`는 출력 파일명(`세특/<식별자>.md`)을 만들어야 하므로
실명을 보관하지만, `audit_logs`와 `artifacts`는 가명(`runs.pseudonym`)만 참조한다.
모델로 나가는 문자열은 `privacy.Pseudonymizer`가 호출 직전에 가명으로 바꾼다.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

DEFAULT_SQLITE_PATH = Path(__file__).resolve().parent / "setuk_c.sqlite3"

# 서버가 죽은 뒤 남은 RUNNING 작업을 회수하기까지 기다리는 시간.
LEASE_SECONDS = 120

STATUS_RUNNING = "running"
STATUS_AWAITING_REVIEW = "awaiting_review"
STATUS_DONE = "done"
STATUS_LINT_FAILED = "lint_failed"
STATUS_ERROR = "error"
STATUS_INTERRUPTED = "interrupted"
# 모델이 정보 부족을 선언했거나, 계약 복구가 실패해 사람이 봐야 하는 상태.
# `error`(코드가 처리할 수 없는 고장)와 구분한다 — 이쪽은 교사가 입력을 채우면 풀린다.
STATUS_NEEDS_INPUT = "needs_input"

TERMINAL_STATUSES = frozenset(
    {STATUS_DONE, STATUS_LINT_FAILED, STATUS_ERROR, STATUS_NEEDS_INPUT}
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Run(Base):
    """하나의 세특 작성 작업. id가 곧 LangGraph thread_id다."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    student_key: Mapped[str] = mapped_column(String(128))
    pseudonym: Mapped[str] = mapped_column(String(32), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), index=True)

    report_path: Mapped[str | None] = mapped_column(String(512), default=None)
    yaml_path: Mapped[str | None] = mapped_column(String(512), default=None)
    output_path: Mapped[str | None] = mapped_column(String(512), default=None)

    draft_text: Mapped[str | None] = mapped_column(Text, default=None)
    draft_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    lint_summary: Mapped[str | None] = mapped_column(Text, default=None)
    lint_retry_count: Mapped[int] = mapped_column(Integer, default=0)
    lint_passed: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # 사람이 채워야 하는 공백(모델의 질문 또는 계약 복구 실패 사유). error와 구분한다.
    needs_input: Mapped[dict | None] = mapped_column(JSON, default=None)
    warnings: Mapped[list[str] | None] = mapped_column(JSON, default=None)

    # 어떤 지침·모델·규정으로 만든 결과인지 나중에 재현할 수 있어야 한다.
    prompt_version: Mapped[str | None] = mapped_column(String(64), default=None)
    model_name: Mapped[str | None] = mapped_column(String(64), default=None)
    # 규정은 학년도마다 개정된다. 어느 스냅샷으로 검증했는지 남겨야 나중에 "그때는 맞았다"를
    # 증명할 수 있고, 코퍼스가 갱신되면 불일치를 감지할 수 있다.
    policy_index_version: Mapped[str | None] = mapped_column(String(64), default=None)
    policy_year: Mapped[int | None] = mapped_column(Integer, default=None)
    policy_findings: Mapped[list[dict] | None] = mapped_column(JSON, default=None)

    # 프로세스가 죽었는지 판별하는 리스. 만료되면 복구 워커가 회수한다.
    lease_owner: Mapped[str | None] = mapped_column(String(64), default=None)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    # 오래된 화면에서 온 승인이 최신 초안을 덮어쓰지 못하게 하는 낙관적 잠금.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version}

    reviews: Mapped[list["Review"]] = relationship(back_populates="run", cascade="all, delete-orphan")

    def is_lease_expired(self, now: datetime | None = None) -> bool:
        if self.lease_expires_at is None:
            return True
        moment = now or utcnow()
        expires = self.lease_expires_at
        if expires.tzinfo is None:  # SQLite는 tz를 보존하지 않는다
            expires = expires.replace(tzinfo=timezone.utc)
        return expires < moment


class Review(Base):
    """교사의 승인·반려 한 건. 같은 버튼을 두 번 눌러도 한 건만 남는다."""

    __tablename__ = "reviews"
    __table_args__ = (UniqueConstraint("run_id", "idempotency_key", name="uq_review_idempotency"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))

    decision: Mapped[str] = mapped_column(String(16))  # approve | reject
    reviewer: Mapped[str] = mapped_column(String(128), default="local-teacher")
    reason: Mapped[str | None] = mapped_column(Text, default=None)

    # 어떤 초안에 대한 승인인지 원문 없이 특정한다.
    draft_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    approved_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    edited: Mapped[bool] = mapped_column(Boolean, default=False)

    prompt_version: Mapped[str | None] = mapped_column(String(64), default=None)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="reviews")


class Artifact(Base):
    """단계별 중간 산출물. 가명 기준으로만 조회한다."""

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # structured_facts | draft | final
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    """누가 언제 무엇을 했는지. 학생 실명과 프롬프트 전문은 넣지 않는다."""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: uuid.uuid4().hex)
    run_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    pseudonym: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    actor: Mapped[str] = mapped_column(String(64))  # system | teacher | model
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class PseudonymMap(Base):
    """실행 하나의 실명 ↔ 별칭 치환표.

    의도적으로 LangGraph 체크포인트(run_state_c.sqlite3)가 아니라 이 앱 DB에 둔다.
    체크포인트는 그래프 실행 상태(interrupt/resume에 쓰는 state)를 위한 저장소이고,
    이 표는 그 상태와는 다른 생명주기·접근 경로를 가져야 "원본과 매핑을 분리 저장한다"는
    요건에 실질적인 의미가 생긴다.
    """

    __tablename__ = "pseudonym_maps"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mapping: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


def database_url() -> str:
    configured = os.environ.get("DATABASE_URL", "").strip()
    if configured:
        return configured
    return f"sqlite:///{DEFAULT_SQLITE_PATH}"


def checkpointer_dsn(url: str | None = None) -> str | None:
    """LangGraph PostgresSaver에 넘길 DSN. SQLite면 None(=SqliteSaver 사용)."""
    target = url or database_url()
    if not target.startswith("postgres"):
        return None
    # SQLAlchemy 드라이버 접미사는 psycopg가 이해하지 못한다.
    return target.replace("postgresql+psycopg://", "postgresql://").replace(
        "postgresql+psycopg2://", "postgresql://"
    )


def create_db_engine(url: str | None = None):
    target = url or database_url()
    kwargs: dict[str, Any] = {"future": True}
    if target.startswith("sqlite"):
        # Flask가 요청마다 다른 스레드를 쓴다. 파이프라인 스레드도 같은 엔진을 공유한다.
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(target, **kwargs)


class Database:
    """엔진과 세션 팩토리를 묶어 서버·테스트가 같은 방식으로 쓰게 한다."""

    def __init__(self, url: str | None = None) -> None:
        self.url = url or database_url()
        self.engine = create_db_engine(self.url)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def create_all(self) -> None:
        """Alembic을 쓰지 않는 로컬 SQLite 기본값을 위한 부트스트랩.

        PostgreSQL 운영 환경에서는 `alembic upgrade head`가 스키마를 관리한다.
        """
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def record_audit(
    session: Session,
    *,
    action: str,
    actor: str = "system",
    run_id: str | None = None,
    pseudonym: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditLog(
            run_id=run_id,
            pseudonym=pseudonym,
            actor=actor,
            action=action,
            detail=detail,
        )
    )


def claim_lease(run: Run, owner: str, seconds: int = LEASE_SECONDS) -> None:
    run.lease_owner = owner
    run.lease_expires_at = utcnow() + timedelta(seconds=seconds)


def release_lease(run: Run) -> None:
    run.lease_owner = None
    run.lease_expires_at = None


def recover_stale_runs(db: Database, owner: str, seconds: int = LEASE_SECONDS) -> list[str]:
    """리스가 만료된 RUNNING 작업을 회수한다.

    프로세스가 죽으면 그 작업은 영원히 RUNNING으로 남는다. 서버 기동 시와 주기적으로
    호출해 `interrupted`로 내려두면, 교사가 목록에서 보고 다시 시작할 수 있다.
    체크포인트는 그대로 있으므로 승인 대기 지점부터 재개된다.
    """
    recovered: list[str] = []
    with db.session() as session:
        stale = session.scalars(select(Run).where(Run.status == STATUS_RUNNING)).all()
        for run in stale:
            if run.lease_owner == owner or not run.is_lease_expired():
                continue
            run.status = STATUS_INTERRUPTED
            run.error = (
                "서버가 이 작업을 처리하던 중 중단되었습니다. "
                "저장된 체크포인트에서 이어서 실행할 수 있습니다."
            )
            release_lease(run)
            recovered.append(run.id)
            record_audit(
                session,
                action="run.recovered",
                run_id=run.id,
                pseudonym=run.pseudonym,
                detail={"previous_status": STATUS_RUNNING, "lease_seconds": seconds},
            )
    return recovered


class DatabasePseudonymStore:
    """`pipeline.PseudonymStore` 프로토콜의 DB 구현. server.py가 기동 시 연결한다."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, run_id: str, mapping: dict[str, str]) -> None:
        with self.db.session() as session:
            row = session.get(PseudonymMap, run_id)
            if row is None:
                session.add(PseudonymMap(run_id=run_id, mapping=dict(mapping)))
            else:
                row.mapping = dict(mapping)

    def load(self, run_id: str) -> dict[str, str]:
        with self.db.session() as session:
            row = session.get(PseudonymMap, run_id)
            return dict(row.mapping) if row is not None else {}
