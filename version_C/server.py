"""로컬 웹 UI. 작업 목록·검토 이력·감사 로그는 전부 데이터베이스에서 읽는다.

v1 대비 달라진 점:

- `_RUNS` 메모리 딕셔너리를 없앴다. run 메타데이터는 `db.Run`, 진행 상태는 LangGraph
  체크포인트이며 **둘 다 같은 식별자(run.id == thread_id)** 를 쓴다. 서버를 재시작해도
  목록에서 작업을 찾아 이어서 실행할 수 있다.
- 승인 버튼을 두 번 눌러도 resume이 두 번 실행되지 않는다(`reviews`의 idempotency key).
- 오래된 화면에서 승인하면 최신 초안을 덮어쓰지 않고 409로 거절한다(초안 해시 + 낙관적 잠금).
- 프로세스가 죽어 RUNNING으로 굳은 작업은 리스 만료 후 복구 워커가 회수한다.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from flask import Flask, abort, redirect, render_template_string, request, url_for
from langgraph.types import Command
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from . import db as dbmod
from . import privacy
from .checkpointer import open_checkpointer
from .db import (
    STATUS_AWAITING_REVIEW,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_INTERRUPTED,
    STATUS_LINT_FAILED,
    STATUS_NEEDS_INPUT,
    STATUS_RUNNING,
    Database,
    Review,
    Run,
    claim_lease,
    record_audit,
    recover_stale_runs,
    release_lease,
    utcnow,
)
from . import pipeline
from .pipeline import build_graph
from .prompts import prompt_version

load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("setuk.server")

app = Flask(__name__)

# 이 프로세스를 가리키는 이름. 리스 소유자를 구분하는 데 쓴다.
WORKER_ID = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
RECOVERY_INTERVAL_SECONDS = 60

_stack = ExitStack()
atexit.register(_stack.close)

db = Database()
db.create_all()
pipeline.configure_pseudonym_store(dbmod.DatabasePseudonymStore(db))

_checkpointer, CHECKPOINT_BACKEND = open_checkpointer(_stack)
_graph = build_graph(_checkpointer)

logger.info("worker=%s db=%s checkpointer=%s", WORKER_ID, db.url, CHECKPOINT_BACKEND)


def policy_service():
    """규정 서비스. 코퍼스가 없어도 서버는 떠야 하므로 실패는 None으로 흡수한다."""
    try:
        from .policy.service import get_service

        return get_service()
    except Exception:
        logger.exception("규정 코퍼스를 불러오지 못했습니다. 규정 인용 없이 계속합니다.")
        return None


def active_policy_year() -> int | None:
    """이 실행에 적용할 학년도. 미설정이면 코퍼스의 최신 학년도를 쓴다."""
    configured = os.environ.get("SETUK_POLICY_YEAR", "").strip()
    if configured.isdigit():
        return int(configured)
    service = policy_service()
    return service.manifest.latest_policy_year if service else None


# --------------------------------------------------------------------------- 상태 반영


def _apply_result(run_id: str, state: dict[str, Any]) -> None:
    """그래프 실행 결과를 run 행에 반영한다."""
    with db.session() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        interrupts = state.get("__interrupt__")
        run.warnings = state.get("warnings") or None
        # 어느 규정 스냅샷으로 검증했는지 결과와 함께 고정한다.
        if state.get("policy_index_version"):
            run.policy_index_version = state["policy_index_version"]
        if state.get("policy_findings") is not None:
            run.policy_findings = state["policy_findings"] or None

        if interrupts:
            payload = interrupts[0].value
            run.status = STATUS_AWAITING_REVIEW
            run.draft_text = payload.get("draft_text", "")
            run.draft_hash = payload.get("draft_hash") or privacy.content_hash(run.draft_text)
            action = "run.awaiting_review"
        elif state.get("needs_input"):
            # 계약 복구 실패나 정보 부족은 고장이 아니라 사람이 채워야 하는 공백이다.
            run.status = STATUS_NEEDS_INPUT
            run.needs_input = state["needs_input"]
            run.error = None
            action = "run.needs_input"
        elif state.get("error"):
            run.status = STATUS_ERROR
            run.error = state["error"]
            action = "run.failed"
        elif state.get("lint_passed"):
            run.status = STATUS_DONE
            run.output_path = state.get("output_path")
            run.lint_summary = state.get("lint_summary")
            run.lint_passed = True
            action = "run.completed"
        else:
            run.status = STATUS_LINT_FAILED
            run.lint_summary = state.get("lint_summary")
            run.lint_retry_count = state.get("lint_retry_count") or 0
            action = "run.lint_failed"

        release_lease(run)
        _store_artifacts(session, run, state)
        record_audit(
            session,
            action=action,
            run_id=run.id,
            pseudonym=run.pseudonym,
            detail={
                "status": run.status,
                "lint_retry_count": run.lint_retry_count,
                # 프롬프트·응답 전문이 아니라 단계별 호출 통계만 남긴다.
                "metrics": state.get("metrics") or [],
                "warnings": run.warnings,
                "policy_index_version": run.policy_index_version,
                "policy_findings": len(run.policy_findings or []),
            },
        )


def _store_artifacts(session, run: Run, state: dict[str, Any]) -> None:
    """단계 산출물을 내용 해시와 함께 남긴다. 같은 내용은 다시 쓰지 않는다."""
    candidates = [
        ("structured_facts", state.get("structured_facts_yaml")),
        ("draft", state.get("draft_text")),
        # 교사가 승인한 본문. draft와 비교하면 "초안 대비 교사 수정 거리"가 나온다.
        ("approved", state.get("reviewed_text")),
        ("final", state.get("final_text")),
    ]
    for kind, content in candidates:
        if not content:
            continue
        digest = privacy.content_hash(content)
        existing = session.scalars(
            select(dbmod.Artifact).where(
                dbmod.Artifact.run_id == run.id,
                dbmod.Artifact.kind == kind,
                dbmod.Artifact.content_hash == digest,
            )
        ).first()
        if existing is None:
            session.add(
                dbmod.Artifact(run_id=run.id, kind=kind, content=content, content_hash=digest)
            )


def _checkpoint_exists(run_id: str) -> bool:
    """이 run에 대응하는 LangGraph 체크포인트가 실제로 있는지 확인한다.

    run 행과 체크포인트는 같은 식별자를 쓰지만 서로 다른 저장소에 있다. 체크포인트
    파일만 지워지거나 DB만 복원되면 둘이 어긋나고, 그대로 resume하면 빈 state로
    그래프가 처음부터 돌면서 `KeyError`가 난다. 재개 전에 여기서 먼저 걸러낸다.
    """
    try:
        snapshot = _graph.get_state({"configurable": {"thread_id": run_id}})
    except Exception:
        logger.exception("체크포인트 조회 실패: run=%s", run_id)
        return False
    return snapshot is not None and snapshot.created_at is not None


def _fail_run(run_id: str, message: str, action: str = "run.failed") -> None:
    with db.session() as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        run.status = STATUS_ERROR
        run.error = message
        release_lease(run)
        record_audit(
            session, action=action, run_id=run.id, pseudonym=run.pseudonym, detail={"reason": message}
        )


class _LeaseHeartbeat:
    """실행이 도는 동안 리스를 주기적으로 갱신한다.

    리스를 시작할 때 한 번만 잡으면 그것은 '살아 있다'가 아니라 '시작한 지 N초가
    지났다'를 뜻할 뿐이다. 세특 한 건은 LLM을 3~4번 부르므로 기본 리스(120초)를
    넘기기 쉬운데, 그러면 **아직 돌고 있는 작업을 다른 워커가 죽은 것으로 보고
    회수해 간다.** 단일 프로세스에서는 소유자 검사에 가려 드러나지 않지만,
    PostgreSQL로 여러 인스턴스를 띄우는 순간 실제로 발생한다.

    스레드를 죽일 수는 없으므로 하트비트가 멈추는 것은 프로세스가 죽었을 때뿐이고,
    그때는 리스가 만료되어 복구 워커가 정상적으로 회수한다.
    """

    def __init__(self, run_id: str, interval: float | None = None):
        self.run_id = run_id
        self.interval = interval or max(5.0, dbmod.LEASE_SECONDS / 3)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _beat(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                with db.session() as session:
                    run = session.get(Run, self.run_id)
                    # 이미 끝났거나 다른 워커가 가져간 작업은 더 붙잡지 않는다.
                    if run is None or run.status != STATUS_RUNNING or run.lease_owner != WORKER_ID:
                        return
                    claim_lease(run, WORKER_ID)
            except Exception:
                logger.debug("리스 갱신 실패: run=%s", self.run_id, exc_info=True)

    def __enter__(self) -> "_LeaseHeartbeat":
        self._thread = threading.Thread(target=self._beat, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()


def _run_graph(run_id: str, payload: dict[str, Any] | None, resume: bool) -> None:
    config = {"configurable": {"thread_id": run_id}}
    if payload is None or resume:
        # 새 입력 없이 이어서 도는 실행. 체크포인트가 없으면 진행할 수 없다.
        if not _checkpoint_exists(run_id):
            _fail_run(
                run_id,
                "이 작업의 체크포인트를 찾을 수 없습니다. 작업 목록과 체크포인트 저장소가 "
                "어긋났을 수 있습니다(체크포인트 파일 삭제 또는 DATABASE_URL 변경). "
                "이 작업은 이어서 진행할 수 없으니 새로 시작하세요.",
                action="run.checkpoint_missing",
            )
            return
    try:
        with _LeaseHeartbeat(run_id):
            if resume:
                state = _graph.invoke(Command(resume=payload), config=config)
            else:
                state = _graph.invoke(payload, config=config)
    except Exception as exc:  # 파이프라인이 어떤 이유로 죽어도 run은 상태를 남겨야 한다
        logger.exception("run %s 실패", run_id)
        _fail_run(run_id, f"{type(exc).__name__}: {exc}")
        return

    try:
        _apply_result(run_id, state)
    except StaleDataError:
        # 결과를 쓰려는 사이 다른 요청이 같은 run을 먼저 바꿨다. 그래프 결과 자체는
        # 체크포인트에 남아 있으므로, 여기서는 리스를 풀고 복구 워커에 맡긴다.
        logger.warning("run %s 결과 반영이 낙관적 잠금에 걸렸습니다. 복구 워커가 처리합니다.", run_id)


def _start_worker(run_id: str, payload: dict[str, Any] | None, resume: bool) -> None:
    threading.Thread(target=_run_graph, args=(run_id, payload, resume), daemon=True).start()


# --------------------------------------------------------------------------- 복구 워커


def _recovery_tick() -> None:
    recovered = recover_stale_runs(db, WORKER_ID)
    if recovered:
        logger.warning("리스가 만료된 작업 %d건을 interrupted로 회수했습니다: %s", len(recovered), recovered)


def _recovery_loop() -> None:
    while True:
        time.sleep(RECOVERY_INTERVAL_SECONDS)
        try:
            _recovery_tick()
        except Exception:  # 복구 워커가 서버를 죽이면 안 된다
            logger.exception("복구 워커 오류")


def _retention_tick() -> None:
    """보존 기간이 지난 데이터를 정리한다. 정책이 꺼져 있으면 아무것도 하지 않는다."""
    from .retention import load_policy, purge_expired

    policy = load_policy()
    if not policy.enabled:
        return
    removed = purge_expired(db, policy)
    if any(removed.values()):
        logger.info("보존 기간이 지난 데이터를 정리했습니다: %s", removed)


def start_recovery_worker() -> None:
    _recovery_tick()  # 기동 시 지난 프로세스가 남긴 작업을 즉시 정리
    try:
        _retention_tick()
    except Exception:  # 보존 정책 오류가 서버 기동을 막지 않게 한다
        logger.exception("보존 기간 정리 실패")
    threading.Thread(target=_recovery_loop, daemon=True).start()


# --------------------------------------------------------------------------- 라우팅


@app.route("/", methods=["GET"])
def index():
    with db.session() as session:
        runs = session.scalars(select(Run).order_by(desc(Run.created_at)).limit(100)).all()
    return render_template_string(
        INDEX_TEMPLATE, runs=runs, backend=CHECKPOINT_BACKEND, db_url=_display_url(db.url)
    )


def _display_url(url: str) -> str:
    """비밀번호가 든 DSN을 화면에 그대로 찍지 않는다."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    return f"{scheme}://***@{rest.rpartition('@')[2]}"


@app.route("/start", methods=["POST"])
def start():
    student_key = request.form.get("student_key", "").strip()
    mode = request.form.get("mode", "")
    if not student_key or mode not in ("docx", "yaml"):
        abort(400, "학생 식별자와 입력 방식을 확인하세요.")

    run_id = uuid.uuid4().hex
    policy_year = active_policy_year()
    payload: dict[str, Any] = {
        "student_key": student_key,
        "mode": mode,
        "policy_year": policy_year,
    }
    report_path = yaml_path = None
    if mode == "docx":
        report_path = request.form.get("report_path", "").strip()
        payload["report_path"] = report_path
    else:
        yaml_path = request.form.get("yaml_path", "").strip()
        payload["yaml_path"] = yaml_path

    with db.session() as session:
        run = Run(
            id=run_id,
            student_key=student_key,
            pseudonym=privacy.pseudonym_for(student_key),
            mode=mode,
            status=STATUS_RUNNING,
            report_path=report_path,
            yaml_path=yaml_path,
            prompt_version=prompt_version(),
            model_name=os.environ.get("ANTHROPIC_MODEL"),
            policy_year=policy_year,
        )
        claim_lease(run, WORKER_ID)
        session.add(run)
        record_audit(
            session,
            action="run.started",
            actor="teacher",
            run_id=run_id,
            pseudonym=run.pseudonym,
            detail={
                "mode": mode,
                "prompt_version": run.prompt_version,
                "policy_year": policy_year,
            },
        )

    _start_worker(run_id, payload, resume=False)
    return redirect(url_for("run_detail", run_id=run_id))


@app.route("/runs/<run_id>", methods=["GET"])
def run_detail(run_id: str):
    with db.session() as session:
        run = session.get(Run, run_id)
        if run is None:
            abort(404, "알 수 없는 작업입니다.")
        reviews = session.scalars(
            select(Review).where(Review.run_id == run_id).order_by(Review.created_at)
        ).all()
    return render_template_string(RUN_TEMPLATE, run=run, reviews=reviews)


@app.route("/runs/<run_id>/review", methods=["POST"])
def review(run_id: str):
    action = request.form.get("action")
    if action not in ("approve", "reject"):
        abort(400, "action은 approve 또는 reject여야 합니다.")

    # 교사가 보고 있던 초안. 그 사이 초안이 바뀌었으면 이 승인은 무효다.
    submitted_hash = request.form.get("draft_hash", "").strip()
    idempotency_key = request.form.get("idempotency_key", "").strip() or uuid.uuid4().hex

    with db.session() as session:
        run = session.get(Run, run_id)
        if run is None:
            abort(404, "알 수 없는 작업입니다.")
        if run.status != STATUS_AWAITING_REVIEW:
            abort(409, f"지금은 검토할 수 있는 상태가 아닙니다(현재: {run.status}).")
        if submitted_hash and run.draft_hash and submitted_hash != run.draft_hash:
            abort(
                409,
                "화면에 열려 있던 초안이 최신 초안과 다릅니다. 새로고침한 뒤 다시 검토하세요.",
            )

        edited_text = request.form.get("edited_text", "")
        reason = request.form.get("reason", "").strip()
        approved = action == "approve"
        approved_text = (edited_text or "").strip() or (run.draft_text or "")

        entry = Review(
            run_id=run_id,
            idempotency_key=idempotency_key,
            decision=action,
            reviewer=_current_reviewer(),
            reason=reason or None,
            draft_hash=run.draft_hash,
            approved_hash=privacy.content_hash(approved_text) if approved else None,
            edited=bool(approved and (edited_text or "").strip() and edited_text.strip() != (run.draft_text or "").strip()),
            prompt_version=run.prompt_version,
            approved_at=utcnow() if approved else None,
        )
        session.add(entry)
        try:
            session.flush()
        except IntegrityError:
            # 같은 버튼을 두 번 눌렀다. 첫 요청이 이미 resume을 걸었으므로 화면만 돌려준다.
            session.rollback()
            logger.info("중복 검토 요청을 무시했습니다: run=%s key=%s", run_id, idempotency_key)
            return redirect(url_for("run_detail", run_id=run_id))

        try:
            run.status = STATUS_RUNNING
            claim_lease(run, WORKER_ID)
            session.flush()
        except StaleDataError:
            session.rollback()
            abort(409, "다른 화면에서 이 작업을 먼저 처리했습니다. 새로고침 후 확인하세요.")

        record_audit(
            session,
            action=f"review.{action}",
            actor="teacher",
            run_id=run_id,
            pseudonym=run.pseudonym,
            detail={
                "reviewer": entry.reviewer,
                "edited": entry.edited,
                "draft_hash": entry.draft_hash,
                "approved_hash": entry.approved_hash,
                "prompt_version": entry.prompt_version,
            },
        )

    if approved:
        payload: dict[str, Any] = {"approved": True, "edited_text": edited_text}
    else:
        payload = {"approved": False, "reason": reason}

    _start_worker(run_id, payload, resume=True)
    return redirect(url_for("run_detail", run_id=run_id))


@app.route("/runs/<run_id>/resume", methods=["POST"])
def resume_interrupted(run_id: str):
    """복구 워커가 회수한 작업을 체크포인트에서 다시 진행시킨다."""
    with db.session() as session:
        run = session.get(Run, run_id)
        if run is None:
            abort(404, "알 수 없는 작업입니다.")
        if run.status != STATUS_INTERRUPTED:
            abort(409, f"중단 상태인 작업만 재개할 수 있습니다(현재: {run.status}).")
        run.status = STATUS_RUNNING
        run.error = None
        claim_lease(run, WORKER_ID)
        record_audit(
            session,
            action="run.resumed",
            actor="teacher",
            run_id=run_id,
            pseudonym=run.pseudonym,
        )

    # 입력값은 체크포인트에 이미 있다. None을 넘기면 중단 지점부터 이어서 실행된다.
    _start_worker(run_id, None, resume=False)
    return redirect(url_for("run_detail", run_id=run_id))


def _current_reviewer() -> str:
    """검토자 식별. 지금은 단일 로컬 사용자이므로 환경변수 또는 고정값을 쓴다.

    학교 단위 배포로 넘어가면 여기가 인증 미들웨어가 붙을 자리다.
    """
    return os.environ.get("SETUK_REVIEWER", "local-teacher")


@app.route("/policy", methods=["GET"])
def policy_search():
    """규정 검색 화면. 교사가 "이거 써도 되나"를 직접 조회할 수 있게 한다."""
    service = policy_service()
    query = request.args.get("q", "").strip()
    hits = []
    if service is not None and query:
        hits = service.search(query, limit=8)
    return render_template_string(
        POLICY_TEMPLATE,
        query=query,
        hits=hits,
        info=service.describe() if service is not None else None,
    )


@app.route("/metrics", methods=["GET"])
def metrics_page():
    """Agent/HITL 지표. 모든 비율은 분모와 함께 보여 준다."""
    from . import metrics as metrics_mod

    window = request.args.get("days", "").strip()
    days = int(window) if window.isdigit() and int(window) > 0 else None
    return render_template_string(
        METRICS_TEMPLATE, data=metrics_mod.collect(db, days=days), days=days
    )


@app.route("/privacy", methods=["GET"])
def privacy_page():
    """보존 정책과 학생별 삭제 화면."""
    from .retention import load_policy

    with db.session() as session:
        runs = session.scalars(select(Run).order_by(desc(Run.created_at)).limit(200)).all()
    students = sorted(
        {(run.student_key, run.pseudonym) for run in runs}, key=lambda item: item[0]
    )
    return render_template_string(
        PRIVACY_TEMPLATE,
        policy=load_policy().describe(),
        students=students,
        deleted=request.args.get("deleted"),
    )


@app.route("/privacy/delete", methods=["POST"])
def privacy_delete():
    """학생별 데이터 삭제(계획서: '학생별 데이터 삭제 API').

    화면에서는 가명으로 지정한다. 삭제 요청 자체가 실명을 로그에 남기지 않게 하기 위함이다.
    """
    from .retention import purge_student

    pseudonym = request.form.get("pseudonym", "").strip()
    if not pseudonym:
        abort(400, "삭제할 학생을 지정하세요.")
    include_audit = request.form.get("include_audit") == "1"
    removed = purge_student(
        db, pseudonym=pseudonym, include_audit=include_audit, actor=_current_reviewer()
    )
    logger.info("학생 데이터 삭제: %s %s", pseudonym, removed)
    return redirect(url_for("privacy_page", deleted=sum(removed.values())))


@app.route("/audit", methods=["GET"])
def audit():
    with db.session() as session:
        entries = session.scalars(
            select(dbmod.AuditLog).order_by(desc(dbmod.AuditLog.created_at)).limit(200)
        ).all()
    return render_template_string(AUDIT_TEMPLATE, entries=entries)


# --------------------------------------------------------------------------- 템플릿

_STYLE = """
<style>
  body { font-family: -apple-system, "Segoe UI", sans-serif; max-width: 860px; margin: 40px auto; color: #1a1a1a; line-height: 1.6; }
  h1 { font-size: 1.4rem; } h2 { font-size: 1.1rem; margin-top: 2rem; }
  textarea { width: 100%; box-sizing: border-box; font-size: 0.95rem; padding: 8px; }
  input[type=text] { width: 100%; box-sizing: border-box; padding: 6px; margin-bottom: 8px; }
  button { padding: 8px 16px; margin-right: 8px; cursor: pointer; }
  .status { display: inline-block; padding: 2px 10px; border-radius: 10px; font-size: 0.85rem; }
  .status-running { background: #fff3cd; } .status-awaiting_review { background: #cfe2ff; }
  .status-done { background: #d1e7dd; }
  .status-error, .status-lint_failed { background: #f8d7da; }
  .status-interrupted { background: #e2e3e5; }
  .status-needs_input { background: #ffe5d0; }
  table { border-collapse: collapse; width: 100%; } td, th { border-bottom: 1px solid #ddd; padding: 6px; text-align: left; vertical-align: top; }
  .warn { color: #842029; background: #f8d7da; padding: 10px; border-radius: 6px; }
  .note { background: #f1f3f5; padding: 10px; border-radius: 6px; font-size: 0.9rem; }
  fieldset { margin-top: 1rem; }
  code, .mono { font-family: Consolas, monospace; font-size: 0.85rem; }
  footer { margin-top: 3rem; font-size: 0.8rem; color: #666; }
</style>
"""

INDEX_TEMPLATE = """
<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>Setuk-AI C버전</title>""" + _STYLE + """
</head><body>
<h1>Setuk-AI C버전 &mdash; 자율 멀티에이전트 (Claude API 직접 호출)</h1>
<p>이 버전은 무설치가 아니며 API 사용량만큼 비용이 발생합니다. 실제 학생 데이터로 쓰기 전에
   <code>학생정보/example.yaml</code> 같은 가상 사례로 먼저 흐름을 확인하세요.</p>

<h2>새 작업 시작</h2>
<form method="post" action="/start">
  <label>학생 식별자 (파일명에 쓸 값)</label>
  <input type="text" name="student_key" required placeholder="예: 김준수">
  <label>입력 방식</label><br>
  <label><input type="radio" name="mode" value="yaml" checked> 기존 YAML 사용</label>
  <input type="text" name="yaml_path" placeholder="예: 학생정보/김준수.yaml"><br>
  <label><input type="radio" name="mode" value="docx"> DOCX 보고서에서 자동 생성</label>
  <input type="text" name="report_path" placeholder="예: 보고서/김준수_주제탐구보고서.docx">
  <button type="submit">시작</button>
</form>

<h2>작업 목록</h2>
{% if not runs %}<p>아직 시작한 작업이 없습니다.</p>{% endif %}
<table>
<tr><th>학생</th><th>상태</th><th>시작</th><th>지침 버전</th><th></th></tr>
{% for run in runs %}
<tr>
  <td>{{ run.student_key }}</td>
  <td><span class="status status-{{ run.status }}">{{ run.status }}</span></td>
  <td class="mono">{{ run.created_at.strftime('%m-%d %H:%M') }}</td>
  <td class="mono">{{ run.prompt_version or '-' }}</td>
  <td><a href="/runs/{{ run.id }}">열기</a></td>
</tr>
{% endfor %}
</table>

<footer>
  작업 목록 저장소: <span class="mono">{{ db_url }}</span> ·
  체크포인터: <span class="mono">{{ backend }}</span> ·
  <a href="/audit">감사 로그</a> ·
  <a href="/policy">규정 검색</a> ·
  <a href="/metrics">지표</a> ·
  <a href="/privacy">개인정보</a>
</footer>
</body></html>
"""

RUN_TEMPLATE = """
<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>{{ run.student_key }} - Setuk-AI C버전</title>
{% if run.status == "running" %}<meta http-equiv="refresh" content="3">{% endif %}
""" + _STYLE + """
</head><body>
<p><a href="/">&larr; 목록으로</a></p>
<h1>{{ run.student_key }} <span class="status status-{{ run.status }}">{{ run.status }}</span></h1>

{% if run.warnings %}
<div class="warn">
  <p><strong>입력 문서 경고</strong></p>
  <ul>{% for item in run.warnings %}<li>{{ item }}</li>{% endfor %}</ul>
  <p>문서 안의 문장은 지시가 아니라 데이터로만 처리했습니다. 내용이 이상하면 원본 보고서를 확인하세요.</p>
</div>
{% endif %}

{% if run.status == "running" %}
  <p>AI가 처리 중입니다. 이 페이지는 3초마다 자동으로 새로고침됩니다.</p>

{% elif run.status == "awaiting_review" %}
  <h2>초안 검토</h2>
  <p>AI가 작성한 초안입니다. 필요하면 아래 내용을 직접 고친 뒤 승인하세요.
     사실 자체(활동 내용·수치·결과)를 새로 만들어 넣지 마세요 &mdash; 사실 관계를 바꾸려면
     반려 사유로 남기고 다시 작성시키는 편이 안전합니다.</p>
  <form method="post" action="/runs/{{ run.id }}/review">
    <input type="hidden" name="action" value="approve">
    <input type="hidden" name="draft_hash" value="{{ run.draft_hash }}">
    <input type="hidden" name="idempotency_key" value="approve-{{ run.draft_hash }}">
    <textarea name="edited_text" rows="10">{{ run.draft_text }}</textarea>
    <button type="submit">이 내용으로 승인</button>
  </form>
  <fieldset>
    <legend>반려하고 다시 작성시키기</legend>
    <form method="post" action="/runs/{{ run.id }}/review">
      <input type="hidden" name="action" value="reject">
      <input type="hidden" name="draft_hash" value="{{ run.draft_hash }}">
      <input type="hidden" name="idempotency_key" value="reject-{{ run.draft_hash }}">
      <input type="text" name="reason" placeholder="반려 사유 (예: 탐구 과정을 더 구체적으로)" required>
      <button type="submit">반려</button>
    </form>
  </fieldset>

{% elif run.status == "done" %}
  <p>완료되었습니다. Linter 오류 0건입니다.</p>
  <p>저장 경로: <code>{{ run.output_path }}</code></p>
  <pre>{{ run.lint_summary }}</pre>
  <p>본문을 검토한 뒤 NEIS에 복사하세요. 이 결과가 최신 학교생활기록부 기재요령을 자동으로
     보증하지 않습니다 &mdash; 최종 확인 책임은 교사에게 있습니다.</p>

{% elif run.status == "lint_failed" %}
  <div class="warn">
    <p>Linter를 {{ run.lint_retry_count }}회 재시도했지만 통과하지 못해 자동 수정을 멈췄습니다.
       아래 결과를 보고 <code>세특/{{ run.student_key }}.md</code>를 직접 확인·수정하세요.</p>
  </div>
  <pre>{{ run.lint_summary }}</pre>

{% elif run.status == "needs_input" %}
  <div class="note">
    <p><strong>AI가 이 작업을 끝내지 못했습니다.</strong> 아래 내용을 확인하고 입력을 보완한 뒤
       새로 시작하세요. 지금까지의 진행 상태는 그대로 남아 있습니다.</p>
    <p>중단 단계: <code>{{ run.needs_input.stage }}</code></p>

    {% if run.needs_input.kind == "questions" %}
      <p>AI가 다음 정보를 요청했습니다:</p>
      <table>
      <tr><th>항목</th><th>질문</th></tr>
      {% for item in run.needs_input.questions %}
      <tr><td class="mono">{{ item.field }}</td><td>{{ item.question }}</td></tr>
      {% endfor %}
      </table>
    {% else %}
      <p>AI 응답이 계약 형식을 만족하지 못했고, 1회 자동 복구도 실패했습니다:</p>
      <pre>{{ run.needs_input.problems }}</pre>
      <p>같은 실패가 반복되면 <code>version_B/.agent/</code>의 지침이나 모델 설정을 확인해야
         합니다. 무한 재시도로 비용을 태우지 않으려고 여기서 멈춥니다.</p>
    {% endif %}
  </div>

{% elif run.status == "interrupted" %}
  <div class="note">
    <p>{{ run.error }}</p>
    <form method="post" action="/runs/{{ run.id }}/resume">
      <button type="submit">체크포인트에서 이어서 실행</button>
    </form>
  </div>

{% elif run.status == "error" %}
  <div class="warn"><p>오류가 발생했습니다: {{ run.error }}</p></div>

{% endif %}

{% if run.policy_findings %}
<h2>규정 점검</h2>
<p class="note">학교생활기록부 기재요령 코퍼스와 대조한 결과입니다. Linter의 통과/실패를
   바꾸지 않는 <strong>참고 정보</strong>이며, 최종 판단은 교사가 원문을 확인해 내려야 합니다.</p>
<table>
<tr><th>등급</th><th>사유</th><th>걸린 표현</th><th>근거</th></tr>
{% for item in run.policy_findings %}
<tr>
  <td>{{ '기재 불가' if item.severity == 'block' else '확인 필요' }}</td>
  <td>{{ item.title }}</td>
  <td class="mono">{{ item.matched }}</td>
  <td>{% if item.source_url %}<a href="{{ item.source_url }}" rel="noopener noreferrer"
      target="_blank">{{ item.citation }}</a>{% else %}{{ item.citation }}{% endif %}</td>
</tr>
{% endfor %}
</table>
{% endif %}

{% if reviews %}
<h2>검토 이력</h2>
<table>
<tr><th>시각</th><th>결정</th><th>검토자</th><th>수정</th><th>초안 해시</th></tr>
{% for item in reviews %}
<tr>
  <td class="mono">{{ item.created_at.strftime('%m-%d %H:%M:%S') }}</td>
  <td>{{ item.decision }}</td>
  <td>{{ item.reviewer }}</td>
  <td>{{ '있음' if item.edited else '-' }}</td>
  <td class="mono">{{ (item.draft_hash or '')[:12] }}</td>
</tr>
{% endfor %}
</table>
{% endif %}

<footer>run id: <span class="mono">{{ run.id }}</span> · 지침 버전:
  <span class="mono">{{ run.prompt_version or '-' }}</span> · 규정:
  <span class="mono">{{ run.policy_year or '-' }}학년도 / {{ run.policy_index_version or '-' }}</span> ·
  가명: <span class="mono">{{ run.pseudonym }}</span></footer>
</body></html>
"""

AUDIT_TEMPLATE = """
<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>감사 로그 - Setuk-AI C버전</title>""" + _STYLE + """
</head><body>
<p><a href="/">&larr; 목록으로</a></p>
<h1>감사 로그</h1>
<p class="note">학생 실명과 프롬프트·응답 전문은 남기지 않습니다. 학생은 가명으로만 식별되며,
   모델 호출은 단계별 호출 횟수·토큰 수·응답 해시로만 기록됩니다.</p>
<table>
<tr><th>시각</th><th>행위자</th><th>동작</th><th>가명</th><th>상세</th></tr>
{% for entry in entries %}
<tr>
  <td class="mono">{{ entry.created_at.strftime('%m-%d %H:%M:%S') }}</td>
  <td>{{ entry.actor }}</td>
  <td class="mono">{{ entry.action }}</td>
  <td class="mono">{{ entry.pseudonym or '-' }}</td>
  <td class="mono">{{ entry.detail }}</td>
</tr>
{% endfor %}
</table>
</body></html>
"""

POLICY_TEMPLATE = """
<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>규정 검색 - Setuk-AI C버전</title>""" + _STYLE + """
</head><body>
<p><a href="/">&larr; 목록으로</a></p>
<h1>학교생활기록부 기재요령 검색</h1>

{% if info is none %}
  <div class="warn"><p>규정 코퍼스를 불러오지 못했습니다.
     <code>version_C/policy_corpus/sources.json</code>을 확인하세요.</p></div>
{% else %}
  <p class="note">
    수록 학년도 {{ info.policy_years | join(', ') }} ·
    조항 {{ info.chunk_count }}건 ·
    검색 방식 <span class="mono">{{ info.retrieval_mode }}</span>
    {% if info.embedding_model %}(<span class="mono">{{ info.embedding_model }}</span>){% endif %} ·
    인덱스 <span class="mono">{{ info.index_version }}</span><br>
    이 코퍼스는 아래 출처에서 만들어졌습니다. <strong>원문 확인 책임은 교사에게 있습니다.</strong>
  </p>
  <table>
  <tr><th>문서</th><th>학년도</th><th>출처 성격</th><th>조항 수</th></tr>
  {% for doc in info.documents %}
  <tr>
    <td>{{ doc.source_name }}</td>
    <td>{{ doc.policy_year }}</td>
    <td>{{ {'official_pdf': '교육부 원문 PDF', 'user_excerpt': '사용자 제공 발췌',
             'project_derived': '프로젝트 자체 규칙'}.get(doc.provenance, doc.provenance) }}</td>
    <td>{{ doc.chunk_count }}</td>
  </tr>
  {% endfor %}
  </table>

  <h2>검색</h2>
  <form method="get" action="/policy">
    <input type="text" name="q" value="{{ query }}" placeholder="예: 교내 대회 수상을 세특에 쓸 수 있나">
    <button type="submit">검색</button>
  </form>

  {% if query and not hits %}<p>검색 결과가 없습니다.</p>{% endif %}
  {% for hit in hits %}
    <fieldset>
      <legend>
        {{ hit.chunk.title }}
        {% if hit.chunk.severity == 'block' %}<strong>[기재 불가]</strong>
        {% elif hit.chunk.severity == 'review' %}[확인 필요]{% endif %}
      </legend>
      <p>{{ hit.chunk.text }}</p>
      <p class="mono">
        근거: {% if hit.chunk.source_url %}<a href="{{ hit.chunk.source_url }}"
          target="_blank" rel="noopener noreferrer">{{ hit.chunk.citation_label() }}</a>
        {% else %}{{ hit.chunk.citation_label() }}{% endif %}
        · id {{ hit.chunk.chunk_id }}
        · 순위 exact={{ hit.exact_rank if hit.exact_rank is not none else '-' }}
          bm25={{ hit.sparse_rank if hit.sparse_rank is not none else '-' }}
          dense={{ hit.dense_rank if hit.dense_rank is not none else '-' }}
      </p>
    </fieldset>
  {% endfor %}
{% endif %}
</body></html>
"""

METRICS_TEMPLATE = """
<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>지표 - Setuk-AI C버전</title>""" + _STYLE + """
</head><body>
<p><a href="/">&larr; 목록으로</a></p>
<h1>Agent / Human-in-the-Loop 지표</h1>

<p class="note">
  <strong>비율은 분모와 함께 읽으세요.</strong> 작업이 몇 건 없을 때의 "반려율 100%"는
  100%가 아니라 "2건 중 2건"입니다. 표본 수가 함께 표시됩니다.<br>
  기간: {% if days %}최근 {{ days }}일{% else %}전체{% endif %} ·
  <a href="/metrics">전체</a> · <a href="/metrics?days=7">7일</a> · <a href="/metrics?days=30">30일</a>
</p>

<h2>작업</h2>
<table>
<tr><th>지표</th><th>값</th></tr>
<tr><td>전체 작업</td><td>{{ data.runs.total }}</td></tr>
<tr><td>진행 중</td><td>{{ data.runs.in_flight }}</td></tr>
<tr><td>성공률</td><td>{{ pct(data.runs.success_rate) }}</td></tr>
<tr><td>상태 분포</td><td class="mono">{{ data.runs.by_status }}</td></tr>
</table>

<h2>교사 검토</h2>
<table>
<tr><th>지표</th><th>값</th></tr>
<tr><td>검토 결정 수</td><td>{{ data.human_review.decisions }}</td></tr>
<tr><td>반려율</td><td>{{ pct(data.human_review.rejection_rate) }}</td></tr>
<tr><td>승인 시 수정률</td><td>{{ pct(data.human_review.edit_rate) }}</td></tr>
<tr><td>초안 대비 수정 거리(중앙값)</td>
    <td>{{ num(data.human_review.median_edit_distance) }}
        <span class="mono">(표본 {{ data.human_review.edit_distance_samples }})</span></td></tr>
<tr><td>초안→승인 소요(중앙값)</td>
    <td>{{ secs(data.human_review.median_seconds_to_approval) }}
        <span class="mono">(표본 {{ data.human_review.approval_samples }})</span></td></tr>
</table>
<p class="note">수정 거리는 0.0(그대로 승인)에서 1.0(완전히 새로 씀) 사이입니다. 이 값이 높게
   유지되면 초안 품질이나 지침을 손봐야 한다는 신호입니다.</p>

<h2>파이프라인</h2>
<table>
<tr><th>지표</th><th>값</th></tr>
<tr><td>평균 Linter 재시도</td><td>{{ num(data.pipeline.mean_lint_retries) }}</td></tr>
<tr><td>입력 토큰</td><td>{{ "{:,}".format(data.pipeline.input_tokens) }}</td></tr>
<tr><td>출력 토큰</td><td>{{ "{:,}".format(data.pipeline.output_tokens) }}</td></tr>
</table>

{% if data.pipeline.stage_failure_rate %}
<h3>단계별 계약 검증 실패율</h3>
<table>
<tr><th>단계</th><th>실패율</th></tr>
{% for stage, rate in data.pipeline.stage_failure_rate.items() %}
<tr><td class="mono">{{ stage }}</td><td>{{ pct(rate) }}</td></tr>
{% endfor %}
</table>
{% endif %}

<h2>재시작 복구</h2>
<table>
<tr><th>지표</th><th>값</th></tr>
<tr><td>회수된 작업</td><td>{{ data.recovery.recovered_runs }}</td></tr>
<tr><td>회수 후 종료까지 도달</td><td>{{ pct(data.recovery.resolved_after_recovery) }}</td></tr>
<tr><td>아직 중단 상태</td><td>{{ data.recovery.still_interrupted }}</td></tr>
</table>

<footer>생성 시각 <span class="mono">{{ data.generated_at }}</span></footer>
</body></html>
"""

PRIVACY_TEMPLATE = """
<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>개인정보 - Setuk-AI C버전</title>""" + _STYLE + """
</head><body>
<p><a href="/">&larr; 목록으로</a></p>
<h1>개인정보 보존과 삭제</h1>

{% if deleted %}<p class="note">삭제 완료: {{ deleted }}개 항목을 지웠습니다.</p>{% endif %}

<h2>보존 정책</h2>
<table>
<tr><th>대상</th><th>보존 기간</th></tr>
<tr><td>초안 본문 (<code>runs.draft_text</code>)</td><td>{{ policy.draft_text }}</td></tr>
<tr><td>산출물 (<code>artifacts</code>)</td><td>{{ policy.artifacts }}</td></tr>
<tr><td>감사 로그 (<code>audit_logs</code>)</td><td>{{ policy.audit_logs }}</td></tr>
</table>
<p class="note">
  {% if not policy.enabled %}
  자동 삭제가 꺼져 있습니다. <code>.env</code>의 <code>SETUK_RETENTION_DAYS</code>로 켤 수 있습니다.
  {% else %}
  보존 기간이 지난 데이터는 서버 기동 시 자동으로 정리됩니다.
  {% endif %}
  감사 로그는 기본적으로 지우지 않습니다 &mdash; <strong>무엇을 언제 지웠는지를 증명하는 기록</strong>이며
  가명과 해시만 담고 있어 실명을 되살리지 않습니다.
</p>

<h2>학생별 데이터 삭제</h2>
<p>지우는 것: 초안 본문, 산출물, 검토 이력, 작업 기록.<br>
   <strong>지우지 않는 것: <code>세특/&lt;식별자&gt;.md</code> 결과 파일</strong> &mdash;
   교사가 NEIS에 옮겨 적을 산출물이므로 파일 관리는 교사 몫입니다.</p>

{% if not students %}<p>삭제할 데이터가 없습니다.</p>{% endif %}
<table>
<tr><th>학생</th><th>가명</th><th></th></tr>
{% for student_key, pseudonym in students %}
<tr>
  <td>{{ student_key }}</td>
  <td class="mono">{{ pseudonym }}</td>
  <td>
    <form method="post" action="/privacy/delete"
          onsubmit="return confirm('{{ student_key }}의 초안·산출물·검토 이력을 지웁니다. 되돌릴 수 없습니다.');">
      <input type="hidden" name="pseudonym" value="{{ pseudonym }}">
      <label><input type="checkbox" name="include_audit" value="1"> 감사 로그까지</label>
      <button type="submit">삭제</button>
    </form>
  </td>
</tr>
{% endfor %}
</table>
</body></html>
"""


@app.template_global()
def pct(ratio):
    """비율을 분모와 함께 표시한다. 표본이 없으면 계산하지 않는다."""
    if not ratio or ratio.get("value") is None:
        return f"— (표본 {ratio.get('of', 0) if ratio else 0})"
    return f"{ratio['value'] * 100:.1f}% ({ratio['count']}/{ratio['of']})"


@app.template_global()
def num(value):
    return "—" if value is None else f"{value}"


@app.template_global()
def secs(value):
    if value is None:
        return "—"
    if value < 60:
        return f"{value:.0f}초"
    if value < 3600:
        return f"{value / 60:.1f}분"
    return f"{value / 3600:.1f}시간"
