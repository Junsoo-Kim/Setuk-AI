from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# server는 임포트 시점에 DB와 체크포인터를 연다. 실제 작업 파일을 건드리지 않도록
# 임시 경로를 먼저 지정한다.
_TMP = tempfile.TemporaryDirectory()
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_TMP.name) / 'test_runs.sqlite3'}"
os.environ["SETUK_CHECKPOINT_PATH"] = str(Path(_TMP.name) / "test_checkpoints.sqlite3")

from version_C import privacy, server  # noqa: E402
from version_C.db import (  # noqa: E402
    STATUS_AWAITING_REVIEW,
    STATUS_DONE,
    STATUS_INTERRUPTED,
    STATUS_NEEDS_INPUT,
    STATUS_RUNNING,
    Review,
    Run,
    claim_lease,
    recover_stale_runs,
    utcnow,
)
from sqlalchemy import select  # noqa: E402


def make_run(run_id: str, **overrides) -> Run:
    student_key = overrides.pop("student_key", "테스트")
    defaults = dict(
        id=run_id,
        student_key=student_key,
        pseudonym=privacy.pseudonym_for(student_key),
        mode="yaml",
        status=STATUS_RUNNING,
    )
    defaults.update(overrides)
    return Run(**defaults)


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        server.app.testing = True
        self.client = server.app.test_client()
        with server.db.session() as session:
            for table in ("audit_logs", "artifacts", "reviews", "runs"):
                session.execute(__import__("sqlalchemy").text(f"DELETE FROM {table}"))
        self.login_as("test-admin", "admin")

    def login_as(self, username: str, role: str) -> None:
        with self.client.session_transaction() as flask_session:
            flask_session["username"] = username
            flask_session["role"] = role


class ServerRoutingTests(ServerTestCase):
    def test_index_page_loads(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Setuk-AI C버전", response.get_data(as_text=True))

    def test_start_without_student_key_is_rejected(self):
        response = self.client.post("/start", data={"mode": "yaml"})
        self.assertEqual(response.status_code, 400)

    def test_unknown_run_detail_is_404(self):
        response = self.client.get("/runs/does-not-exist")
        self.assertEqual(response.status_code, 404)

    def test_review_before_awaiting_review_is_rejected(self):
        with server.db.session() as session:
            session.add(make_run("fixed-id"))
        response = self.client.post(
            "/runs/fixed-id/review", data={"action": "approve", "edited_text": ""}
        )
        self.assertEqual(response.status_code, 409)

    def test_start_with_missing_docx_ends_in_error_status(self):
        response = self.client.post(
            "/start",
            data={"mode": "docx", "student_key": "없는학생", "report_path": "보고서/없음.docx"},
        )
        self.assertEqual(response.status_code, 302)
        run_id = response.headers["Location"].rsplit("/", 1)[-1]

        info = self._wait_for_status(run_id, exclude=STATUS_RUNNING)
        self.assertEqual(info.status, "error")
        self.assertIn("DOCX", info.error)

    def _wait_for_status(self, run_id: str, exclude: str) -> Run:
        for _ in range(50):
            with server.db.session() as session:
                run = session.get(Run, run_id)
            if run is not None and run.status != exclude:
                return run
            time.sleep(0.1)
        raise AssertionError(f"{run_id}가 {exclude} 상태에서 벗어나지 않았습니다.")


class RunPersistenceTests(ServerTestCase):
    """작업 목록이 프로세스 메모리가 아니라 DB에서 온다는 것을 확인한다."""

    def test_run_list_is_read_from_database(self):
        with server.db.session() as session:
            session.add(make_run("persisted-1", student_key="영속학생", status=STATUS_DONE))
        response = self.client.get("/")
        self.assertIn("영속학생", response.get_data(as_text=True))

    def test_run_detail_survives_without_any_in_memory_state(self):
        with server.db.session() as session:
            session.add(
                make_run("persisted-2", status=STATUS_DONE, output_path="세특/테스트.md")
            )
        response = self.client.get("/runs/persisted-2")
        self.assertEqual(response.status_code, 200)
        self.assertIn("세특/테스트.md", response.get_data(as_text=True))

    def test_server_module_has_no_in_memory_run_registry(self):
        self.assertFalse(hasattr(server, "_RUNS"))


class ReviewIdempotencyTests(ServerTestCase):
    def _awaiting_run(self, run_id: str, draft: str = "초안 본문") -> str:
        digest = privacy.content_hash(draft)
        with server.db.session() as session:
            session.add(
                make_run(
                    run_id,
                    status=STATUS_AWAITING_REVIEW,
                    draft_text=draft,
                    draft_hash=digest,
                )
            )
        return digest

    def test_duplicate_approval_creates_only_one_review(self):
        digest = self._awaiting_run("dup-1")
        form = {
            "action": "approve",
            "edited_text": "",
            "draft_hash": digest,
            "idempotency_key": f"approve-{digest}",
        }
        first = self.client.post("/runs/dup-1/review", data=form)
        self.assertEqual(first.status_code, 302)

        # 두 번째 클릭은 상태가 이미 running이므로 409로 막힌다.
        second = self.client.post("/runs/dup-1/review", data=form)
        self.assertEqual(second.status_code, 409)

        with server.db.session() as session:
            reviews = session.scalars(select(Review).where(Review.run_id == "dup-1")).all()
        self.assertEqual(len(reviews), 1)

    def test_same_idempotency_key_is_not_recorded_twice(self):
        digest = self._awaiting_run("dup-2")
        with server.db.session() as session:
            session.add(
                Review(
                    run_id="dup-2",
                    idempotency_key=f"approve-{digest}",
                    decision="approve",
                    reviewer="local-teacher",
                    draft_hash=digest,
                )
            )
        response = self.client.post(
            "/runs/dup-2/review",
            data={
                "action": "approve",
                "edited_text": "",
                "draft_hash": digest,
                "idempotency_key": f"approve-{digest}",
            },
        )
        self.assertEqual(response.status_code, 302)
        with server.db.session() as session:
            reviews = session.scalars(select(Review).where(Review.run_id == "dup-2")).all()
            run = session.get(Run, "dup-2")
        self.assertEqual(len(reviews), 1)
        # 중복 요청은 resume을 걸지 않으므로 상태가 그대로 남는다.
        self.assertEqual(run.status, STATUS_AWAITING_REVIEW)

    def test_stale_draft_hash_is_rejected(self):
        self._awaiting_run("stale-1", draft="최신 초안")
        response = self.client.post(
            "/runs/stale-1/review",
            data={
                "action": "approve",
                "edited_text": "",
                "draft_hash": privacy.content_hash("교사 화면에 남아 있던 옛 초안"),
                "idempotency_key": "approve-old",
            },
        )
        self.assertEqual(response.status_code, 409)
        with server.db.session() as session:
            run = session.get(Run, "stale-1")
        self.assertEqual(run.status, STATUS_AWAITING_REVIEW)

    def test_review_records_reviewer_and_approved_hash(self):
        digest = self._awaiting_run("audit-1", draft="원본 초안")
        self.client.post(
            "/runs/audit-1/review",
            data={
                "action": "approve",
                "edited_text": "교사가 고친 초안",
                "draft_hash": digest,
                "idempotency_key": f"approve-{digest}",
            },
        )
        with server.db.session() as session:
            review = session.scalars(select(Review).where(Review.run_id == "audit-1")).one()
        self.assertEqual(review.reviewer, "test-admin")
        self.assertTrue(review.edited)
        self.assertIsNotNone(review.approved_at)
        self.assertEqual(review.approved_hash, privacy.content_hash("교사가 고친 초안"))


class RecoveryTests(ServerTestCase):
    def test_expired_lease_moves_running_run_to_interrupted(self):
        with server.db.session() as session:
            run = make_run("stale-run")
            claim_lease(run, "dead-worker", seconds=-1)  # 이미 만료된 리스
            session.add(run)

        recovered = recover_stale_runs(server.db, "this-worker")
        self.assertIn("stale-run", recovered)

        with server.db.session() as session:
            run = session.get(Run, "stale-run")
        self.assertEqual(run.status, STATUS_INTERRUPTED)
        self.assertIsNone(run.lease_owner)

    def test_live_lease_is_left_alone(self):
        with server.db.session() as session:
            run = make_run("live-run")
            claim_lease(run, "other-worker", seconds=300)
            session.add(run)

        recovered = recover_stale_runs(server.db, "this-worker")
        self.assertNotIn("live-run", recovered)
        with server.db.session() as session:
            run = session.get(Run, "live-run")
        self.assertEqual(run.status, STATUS_RUNNING)

    def test_interrupted_run_exposes_resume_action(self):
        with server.db.session() as session:
            session.add(make_run("resume-me", status=STATUS_INTERRUPTED, error="중단됨"))
        page = self.client.get("/runs/resume-me").get_data(as_text=True)
        self.assertIn("/runs/resume-me/resume", page)

    def test_resume_only_allowed_from_interrupted(self):
        with server.db.session() as session:
            session.add(make_run("not-interrupted", status=STATUS_DONE))
        response = self.client.post("/runs/not-interrupted/resume")
        self.assertEqual(response.status_code, 409)


class NeedsInputTests(ServerTestCase):
    """계약 복구 실패나 정보 부족은 고장(error)이 아니라 사람이 채워야 하는 공백이다."""

    def test_pipeline_error_field_sets_needs_input_status(self):
        with server.db.session() as session:
            session.add(make_run("ni-1", status=STATUS_RUNNING))
        payload = {
            "stage": "02_data_structuring",
            "kind": "contract_validation",
            "questions": [],
            "problems": "- STRUCTURED_FACTS_V1.output_path: Field required",
        }
        server._apply_result("ni-1", {"needs_input": payload})
        with server.db.session() as session:
            run = session.get(Run, "ni-1")
        self.assertEqual(run.status, STATUS_NEEDS_INPUT)
        self.assertEqual(run.needs_input["stage"], "02_data_structuring")
        self.assertIsNone(run.error)

    def test_needs_input_run_detail_shows_questions(self):
        with server.db.session() as session:
            session.add(
                make_run(
                    "ni-2",
                    status=STATUS_NEEDS_INPUT,
                    needs_input={
                        "stage": "02_data_structuring",
                        "kind": "questions",
                        "questions": [{"field": "activities[0].process", "question": "무엇을 했나요?"}],
                        "problems": None,
                    },
                )
            )
        body = self.client.get("/runs/ni-2").get_data(as_text=True)
        self.assertIn("activities[0].process", body)
        self.assertIn("무엇을 했나요?", body)

    def test_needs_input_run_detail_shows_contract_problems(self):
        with server.db.session() as session:
            session.add(
                make_run(
                    "ni-3",
                    status=STATUS_NEEDS_INPUT,
                    needs_input={
                        "stage": "03_drafting",
                        "kind": "contract_validation",
                        "questions": [],
                        "problems": "- DRAFT_V1.text: Field required",
                    },
                )
            )
        body = self.client.get("/runs/ni-3").get_data(as_text=True)
        self.assertIn("DRAFT_V1.text", body)
        self.assertIn("03_drafting", body)


class CheckpointDriftTests(ServerTestCase):
    """run 행은 있는데 체크포인트가 없는 경우(저장소 불일치)를 읽을 수 있는 오류로 바꾼다."""

    def test_resume_without_checkpoint_reports_a_legible_error(self):
        draft = "체크포인트 없는 초안"
        digest = privacy.content_hash(draft)
        with server.db.session() as session:
            session.add(
                make_run(
                    "orphan-run",
                    status=STATUS_AWAITING_REVIEW,
                    draft_text=draft,
                    draft_hash=digest,
                )
            )
        response = self.client.post(
            "/runs/orphan-run/review",
            data={
                "action": "approve",
                "edited_text": "",
                "draft_hash": digest,
                "idempotency_key": f"approve-{digest}",
            },
        )
        self.assertEqual(response.status_code, 302)

        run = None
        for _ in range(50):
            with server.db.session() as session:
                run = session.get(Run, "orphan-run")
            if run.status != STATUS_RUNNING:
                break
            time.sleep(0.1)

        self.assertEqual(run.status, "error")
        self.assertIn("체크포인트", run.error)
        self.assertNotIn("KeyError", run.error)

    def test_checkpoint_exists_is_false_for_unknown_thread(self):
        self.assertFalse(server._checkpoint_exists("never-started"))


class LeaseHeartbeatTests(ServerTestCase):
    """리스를 시작할 때 한 번만 잡으면 '살아 있다'가 아니라 '시작한 지 N초 지났다'가 된다.

    세특 한 건은 LLM을 3~4번 부르므로 기본 리스(120초)를 넘기기 쉽고, PostgreSQL로 여러
    인스턴스를 띄우면 아직 돌고 있는 작업을 다른 워커가 회수해 간다.
    """

    def test_heartbeat_extends_the_lease_while_running(self):
        with server.db.session() as session:
            run = make_run("beating", status=STATUS_RUNNING)
            claim_lease(run, server.WORKER_ID, seconds=1)
            session.add(run)
        with server.db.session() as session:
            original = session.get(Run, "beating").lease_expires_at

        with server._LeaseHeartbeat("beating", interval=0.05):
            time.sleep(0.25)

        with server.db.session() as session:
            extended = session.get(Run, "beating").lease_expires_at
        self.assertGreater(extended, original)

    def test_heartbeat_stops_when_the_run_is_no_longer_ours(self):
        with server.db.session() as session:
            run = make_run("not-ours", status=STATUS_RUNNING)
            claim_lease(run, "another-worker", seconds=1)
            session.add(run)
        with server.db.session() as session:
            original = session.get(Run, "not-ours").lease_expires_at

        with server._LeaseHeartbeat("not-ours", interval=0.05):
            time.sleep(0.2)

        with server.db.session() as session:
            after = session.get(Run, "not-ours").lease_expires_at
        self.assertEqual(after, original)

    def test_heartbeat_stops_after_the_run_finishes(self):
        with server.db.session() as session:
            run = make_run("finished", status=STATUS_DONE)
            claim_lease(run, server.WORKER_ID, seconds=1)
            session.add(run)
        with server.db.session() as session:
            original = session.get(Run, "finished").lease_expires_at

        with server._LeaseHeartbeat("finished", interval=0.05):
            time.sleep(0.2)

        with server.db.session() as session:
            after = session.get(Run, "finished").lease_expires_at
        self.assertEqual(after, original)


class GovernancePageTests(ServerTestCase):
    def test_metrics_page_renders_with_no_data(self):
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn("분모와 함께 읽으세요", response.get_data(as_text=True))

    def test_metrics_page_shows_denominator_with_the_ratio(self):
        with server.db.session() as session:
            session.add(make_run("m1", status=STATUS_DONE))
        body = self.client.get("/metrics").get_data(as_text=True)
        self.assertIn("(1/1)", body)

    def test_privacy_page_lists_students_with_pseudonyms(self):
        with server.db.session() as session:
            session.add(make_run("p1", student_key="삭제대상"))
        body = self.client.get("/privacy").get_data(as_text=True)
        self.assertIn("삭제대상", body)
        self.assertIn(privacy.pseudonym_for("삭제대상"), body)

    def test_delete_removes_the_students_runs(self):
        with server.db.session() as session:
            session.add(make_run("p2", student_key="삭제대상"))
        response = self.client.post(
            "/privacy/delete", data={"pseudonym": privacy.pseudonym_for("삭제대상")}
        )
        self.assertEqual(response.status_code, 302)
        with server.db.session() as session:
            self.assertIsNone(session.get(Run, "p2"))

    def test_delete_without_a_target_is_rejected(self):
        self.assertEqual(self.client.post("/privacy/delete", data={}).status_code, 400)


class AuditLogTests(ServerTestCase):
    def test_audit_page_lists_actions_without_real_names(self):
        with server.db.session() as session:
            session.add(make_run("audited", student_key="홍길동", status=STATUS_DONE))
            from version_C.db import record_audit

            record_audit(
                session,
                action="run.completed",
                run_id="audited",
                pseudonym=privacy.pseudonym_for("홍길동"),
                detail={"status": "done"},
            )
        page = self.client.get("/audit").get_data(as_text=True)
        self.assertIn("run.completed", page)
        self.assertIn(privacy.pseudonym_for("홍길동"), page)
        self.assertNotIn("홍길동", page)


class RbacTests(ServerTestCase):
    def test_anonymous_request_is_redirected_to_login(self):
        with self.client.session_transaction() as flask_session:
            flask_session.clear()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_teacher_cannot_open_another_teachers_run(self):
        with server.db.session() as session:
            session.add(make_run("owned-by-a", owner="teacher-a"))
        self.login_as("teacher-b", "teacher")
        response = self.client.get("/runs/owned-by-a")
        self.assertEqual(response.status_code, 403)

    def test_teacher_can_open_their_own_run(self):
        with server.db.session() as session:
            session.add(make_run("owned-by-a", owner="teacher-a"))
        self.login_as("teacher-a", "teacher")
        response = self.client.get("/runs/owned-by-a")
        self.assertEqual(response.status_code, 200)

    def test_teacher_only_sees_their_own_runs_in_the_list(self):
        with server.db.session() as session:
            session.add(make_run("owned-by-a", owner="teacher-a", student_key="학생A"))
            session.add(make_run("owned-by-b", owner="teacher-b", student_key="학생B"))
        self.login_as("teacher-a", "teacher")
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("학생A", body)
        self.assertNotIn("학생B", body)

    def test_admin_sees_every_run(self):
        with server.db.session() as session:
            session.add(make_run("owned-by-a", owner="teacher-a", student_key="학생A"))
            session.add(make_run("owned-by-b", owner="teacher-b", student_key="학생B"))
        self.login_as("the-admin", "admin")
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("학생A", body)
        self.assertIn("학생B", body)

    def test_teacher_cannot_reach_admin_only_pages(self):
        self.login_as("teacher-a", "teacher")
        for path in ("/metrics", "/privacy", "/audit"):
            self.assertEqual(self.client.get(path).status_code, 403)

    def test_real_login_flow_sets_the_session_and_redirects(self):
        from version_C import auth

        with self.client.session_transaction() as flask_session:
            flask_session.clear()
        auth.create_teacher(server.db, "real-login-teacher", "password123", role="teacher")
        response = self.client.post(
            "/login", data={"username": "real-login-teacher", "password": "password123"}
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as flask_session:
            self.assertEqual(flask_session["username"], "real-login-teacher")
            self.assertEqual(flask_session["role"], "teacher")

    def test_real_login_flow_rejects_the_wrong_password(self):
        from version_C import auth

        with self.client.session_transaction() as flask_session:
            flask_session.clear()
        auth.create_teacher(server.db, "real-login-teacher-2", "password123", role="teacher")
        response = self.client.post(
            "/login", data={"username": "real-login-teacher-2", "password": "wrong"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("올바르지 않습니다", response.get_data(as_text=True))

    def test_teacher_cannot_review_another_teachers_run(self):
        with server.db.session() as session:
            session.add(
                make_run("owned-by-a", owner="teacher-a", status=STATUS_AWAITING_REVIEW)
            )
        self.login_as("teacher-b", "teacher")
        response = self.client.post(
            "/runs/owned-by-a/review", data={"action": "approve"}
        )
        self.assertEqual(response.status_code, 403)


def tearDownModule():
    server._stack.close()
    try:
        _TMP.cleanup()
    except (OSError, PermissionError):
        # Windows에서 SQLite 핸들이 남아 있으면 정리를 건너뛴다(임시 디렉터리라 무해).
        pass


if __name__ == "__main__":
    unittest.main()
