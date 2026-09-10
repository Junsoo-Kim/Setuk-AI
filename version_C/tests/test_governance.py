"""보존 기간·학생별 삭제·지표 집계 테스트.

계획서 4장("원본·초안·trace의 보존 기간 설정", "학생별 데이터 삭제 API")과
6장(Agent/HITL 지표)이 요구한 항목들이다.

여기서 지켜야 할 계약:

- 삭제는 실제로 지운다(플래그만 세우고 남겨 두지 않는다).
- 삭제 사실 자체는 감사 로그에 남는다.
- 삭제 요청에 실명을 쓰지 않아도 된다(가명으로 지정 가능).
- 지표의 모든 비율은 분모를 함께 보고한다.
"""

from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from version_C import metrics, privacy, retention
from version_C.db import (
    STATUS_AWAITING_REVIEW,
    STATUS_DONE,
    STATUS_ERROR,
    Artifact,
    AuditLog,
    Database,
    Review,
    Run,
    utcnow,
)


def make_db(case: unittest.TestCase) -> Database:
    """테스트용 인메모리 DB.

    파일 DB를 쓰면 Windows에서 SQLite 핸들이 남아 임시 디렉터리를 지우지 못한다.
    이 테스트들은 단일 스레드라 :memory:로 충분하다.
    """
    db = Database("sqlite:///:memory:")
    db.create_all()
    case.addCleanup(db.engine.dispose)
    return db


def add_run(db: Database, run_id: str, student: str = "테스트", **overrides) -> str:
    defaults = dict(
        id=run_id,
        student_key=student,
        pseudonym=privacy.pseudonym_for(student),
        mode="yaml",
        status=STATUS_DONE,
    )
    defaults.update(overrides)
    with db.session() as session:
        session.add(Run(**defaults))
    return run_id


class RetentionPolicyTests(unittest.TestCase):
    def test_policy_is_disabled_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            policy = retention.load_policy()
        self.assertFalse(policy.enabled)

    def test_single_env_var_sets_draft_and_artifact_days(self):
        with patch.dict("os.environ", {"SETUK_RETENTION_DAYS": "30"}, clear=True):
            policy = retention.load_policy()
        self.assertEqual(policy.draft_days, 30)
        self.assertEqual(policy.artifact_days, 30)

    def test_audit_retention_defaults_to_forever(self):
        """감사 로그는 삭제 사실의 증거다. 함께 지워 버리면 증명할 수 없다."""
        with patch.dict("os.environ", {"SETUK_RETENTION_DAYS": "30"}, clear=True):
            policy = retention.load_policy()
        self.assertEqual(policy.audit_days, 0)
        self.assertEqual(policy.describe()["audit_logs"], "무기한")

    def test_invalid_value_falls_back_instead_of_crashing(self):
        with patch.dict("os.environ", {"SETUK_RETENTION_DAYS": "아무말"}, clear=True):
            policy = retention.load_policy()
        self.assertFalse(policy.enabled)


class PurgeExpiredTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db(self)

    def _age(self, run_id: str, days: int) -> None:
        old = utcnow() - timedelta(days=days)
        with self.db.session() as session:
            run = session.get(Run, run_id)
            run.created_at = old
            for artifact in session.scalars(
                select(Artifact).where(Artifact.run_id == run_id)
            ).all():
                artifact.created_at = old

    def test_disabled_policy_removes_nothing(self):
        add_run(self.db, "r1", draft_text="초안")
        removed = retention.purge_expired(self.db, retention.RetentionPolicy())
        self.assertEqual(sum(removed.values()), 0)
        with self.db.session() as session:
            self.assertIsNotNone(session.get(Run, "r1").draft_text)

    def test_old_draft_text_is_cleared_but_run_row_survives(self):
        """run 행까지 지우면 감사 로그의 run_id가 미아가 된다."""
        add_run(self.db, "r1", draft_text="오래된 초안")
        self._age("r1", 400)
        retention.purge_expired(self.db, retention.RetentionPolicy(draft_days=365))
        with self.db.session() as session:
            run = session.get(Run, "r1")
        self.assertIsNotNone(run)
        self.assertIsNone(run.draft_text)

    def test_recent_draft_is_kept(self):
        add_run(self.db, "r1", draft_text="최근 초안")
        self._age("r1", 10)
        retention.purge_expired(self.db, retention.RetentionPolicy(draft_days=365))
        with self.db.session() as session:
            self.assertIsNotNone(session.get(Run, "r1").draft_text)

    def test_old_artifacts_are_deleted(self):
        add_run(self.db, "r1")
        with self.db.session() as session:
            session.add(
                Artifact(run_id="r1", kind="draft", content="본문", content_hash="h")
            )
        self._age("r1", 400)
        removed = retention.purge_expired(self.db, retention.RetentionPolicy(artifact_days=365))
        self.assertEqual(removed["artifacts"], 1)

    def test_purge_records_an_audit_entry(self):
        add_run(self.db, "r1", draft_text="초안")
        self._age("r1", 400)
        retention.purge_expired(self.db, retention.RetentionPolicy(draft_days=365))
        with self.db.session() as session:
            actions = [log.action for log in session.scalars(select(AuditLog)).all()]
        self.assertIn("retention.purged", actions)


class StudentDeletionTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db(self)
        add_run(self.db, "keep", student="남길학생", draft_text="남는 초안")
        add_run(self.db, "gone", student="지울학생", draft_text="지울 초안")
        with self.db.session() as session:
            session.add(Artifact(run_id="gone", kind="draft", content="x", content_hash="h"))
            session.add(
                Review(run_id="gone", idempotency_key="k", decision="approve", reviewer="t")
            )

    def test_deleting_by_pseudonym_removes_that_students_rows(self):
        removed = retention.purge_student(
            self.db, pseudonym=privacy.pseudonym_for("지울학생")
        )
        self.assertEqual(removed["runs"], 1)
        self.assertEqual(removed["artifacts"], 1)
        self.assertEqual(removed["reviews"], 1)
        with self.db.session() as session:
            self.assertIsNone(session.get(Run, "gone"))

    def test_other_students_data_is_untouched(self):
        retention.purge_student(self.db, pseudonym=privacy.pseudonym_for("지울학생"))
        with self.db.session() as session:
            self.assertIsNotNone(session.get(Run, "keep"))

    def test_deleting_by_real_name_works_too(self):
        removed = retention.purge_student(self.db, student_key="지울학생")
        self.assertEqual(removed["runs"], 1)

    def test_deletion_is_recorded_in_audit_log_without_the_real_name(self):
        retention.purge_student(self.db, pseudonym=privacy.pseudonym_for("지울학생"))
        with self.db.session() as session:
            entries = [
                log
                for log in session.scalars(select(AuditLog)).all()
                if log.action == "retention.student_deleted"
            ]
        self.assertEqual(len(entries), 1)
        self.assertNotIn("지울학생", str(entries[0].detail))
        self.assertEqual(entries[0].pseudonym, privacy.pseudonym_for("지울학생"))

    def test_audit_log_survives_by_default(self):
        target = privacy.pseudonym_for("지울학생")
        with self.db.session() as session:
            session.add(AuditLog(run_id="gone", pseudonym=target, actor="s", action="run.started"))
        retention.purge_student(self.db, pseudonym=target)
        with self.db.session() as session:
            remaining = [
                log.action
                for log in session.scalars(select(AuditLog)).all()
                if log.pseudonym == target
            ]
        self.assertIn("run.started", remaining)

    def test_include_audit_removes_the_log_too(self):
        target = privacy.pseudonym_for("지울학생")
        with self.db.session() as session:
            session.add(AuditLog(run_id="gone", pseudonym=target, actor="s", action="run.started"))
        removed = retention.purge_student(self.db, pseudonym=target, include_audit=True)
        self.assertGreaterEqual(removed["audit_logs"], 1)

    def test_requires_an_identifier(self):
        with self.assertRaises(ValueError):
            retention.purge_student(self.db)


class EditDistanceTests(unittest.TestCase):
    def test_identical_text_has_zero_distance(self):
        self.assertEqual(metrics.normalized_edit_distance("본문", "본문"), 0.0)

    def test_completely_different_text_approaches_one(self):
        self.assertEqual(metrics.normalized_edit_distance("가나다", "ABC"), 1.0)

    def test_small_edit_gives_small_distance(self):
        distance = metrics.normalized_edit_distance(
            "탐구 과정에서 자료를 비교함.", "탐구 과정에서 자료를 대조함."
        )
        self.assertGreater(distance, 0.0)
        self.assertLess(distance, 0.2)

    def test_empty_versus_text(self):
        self.assertEqual(metrics.normalized_edit_distance("", "abc"), 1.0)
        self.assertEqual(metrics.normalized_edit_distance("", ""), 0.0)


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.db = make_db(self)

    def test_empty_database_reports_no_ratios_instead_of_zero(self):
        """표본이 없을 때 0%로 보고하면 '실패가 없다'로 오해된다."""
        data = metrics.collect(self.db)
        self.assertEqual(data["runs"]["total"], 0)
        self.assertIsNone(data["runs"]["success_rate"]["value"])
        self.assertIsNone(data["human_review"]["rejection_rate"]["value"])

    def test_success_rate_counts_only_finished_runs(self):
        add_run(self.db, "done1", status=STATUS_DONE)
        add_run(self.db, "err1", status=STATUS_ERROR)
        add_run(self.db, "waiting", status=STATUS_AWAITING_REVIEW)
        data = metrics.collect(self.db)
        rate = data["runs"]["success_rate"]
        self.assertEqual(rate["of"], 2)  # 진행 중인 작업은 분모에서 빠진다
        self.assertEqual(rate["count"], 1)
        self.assertEqual(data["runs"]["in_flight"], 1)

    def test_every_ratio_reports_its_denominator(self):
        add_run(self.db, "r1")
        with self.db.session() as session:
            session.add(
                Review(run_id="r1", idempotency_key="a", decision="reject", reviewer="t")
            )
        data = metrics.collect(self.db)
        ratio = data["human_review"]["rejection_rate"]
        self.assertEqual(ratio, {"value": 1.0, "count": 1, "of": 1})

    def test_edit_distance_uses_draft_versus_approved_artifacts(self):
        add_run(self.db, "r1")
        with self.db.session() as session:
            session.add(
                Artifact(run_id="r1", kind="draft", content="AI가 쓴 초안", content_hash="a")
            )
            session.add(
                Artifact(
                    run_id="r1", kind="approved", content="교사가 고친 초안", content_hash="b"
                )
            )
        data = metrics.collect(self.db)
        self.assertEqual(data["human_review"]["edit_distance_samples"], 1)
        self.assertGreater(data["human_review"]["median_edit_distance"], 0)

    def test_stage_failure_rate_comes_from_audit_metrics(self):
        add_run(self.db, "r1")
        with self.db.session() as session:
            session.add(
                AuditLog(
                    run_id="r1",
                    actor="system",
                    action="run.completed",
                    detail={
                        "metrics": [
                            {"stage": "draft", "attempts": 2, "repairs": 1,
                             "parse_failures": ["형식 오류"], "input_tokens": 100,
                             "output_tokens": 50},
                        ]
                    },
                )
            )
        data = metrics.collect(self.db)
        self.assertEqual(data["pipeline"]["stage_failure_rate"]["draft"]["of"], 2)
        self.assertEqual(data["pipeline"]["input_tokens"], 100)

    def test_recovery_rate_tracks_runs_that_finished_after_recovery(self):
        add_run(self.db, "r1", status=STATUS_DONE)
        with self.db.session() as session:
            session.add(
                AuditLog(run_id="r1", actor="system", action="run.recovered", detail={})
            )
        data = metrics.collect(self.db)
        self.assertEqual(data["recovery"]["recovered_runs"], 1)
        self.assertEqual(data["recovery"]["resolved_after_recovery"]["value"], 1.0)

    def test_window_filters_older_runs(self):
        add_run(self.db, "old")
        with self.db.session() as session:
            session.get(Run, "old").created_at = utcnow() - timedelta(days=100)
        add_run(self.db, "new")
        self.assertEqual(metrics.collect(self.db, days=7)["runs"]["total"], 1)
        self.assertEqual(metrics.collect(self.db)["runs"]["total"], 2)


class TokenBudgetTests(unittest.TestCase):
    def setUp(self):
        from version_C import pipeline

        self.pipeline = pipeline

    def test_budget_is_off_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(self.pipeline.token_budget(), 0)
            self.assertIsNone(self.pipeline._budget_exceeded({"metrics": [
                {"input_tokens": 10**9, "output_tokens": 10**9}
            ]}))

    def test_budget_stops_the_run_when_exceeded(self):
        state = {"metrics": [{"input_tokens": 600, "output_tokens": 600}]}
        with patch.dict("os.environ", {"SETUK_MAX_TOKENS_PER_RUN": "1000"}, clear=True):
            message = self.pipeline._budget_exceeded(state)
        self.assertIsNotNone(message)
        self.assertIn("1,000", message)

    def test_budget_allows_runs_under_the_limit(self):
        state = {"metrics": [{"input_tokens": 100, "output_tokens": 100}]}
        with patch.dict("os.environ", {"SETUK_MAX_TOKENS_PER_RUN": "1000"}, clear=True):
            self.assertIsNone(self.pipeline._budget_exceeded(state))

    def test_draft_node_refuses_to_call_the_model_over_budget(self):
        state = {
            "metrics": [{"input_tokens": 5000, "output_tokens": 5000}],
            "structured_facts_yaml": "contract: STRUCTURED_FACTS_V1\n",
        }

        def must_not_be_called(*args, **kwargs):
            raise AssertionError("예산을 넘겼는데도 모델을 호출했습니다.")

        with patch.dict("os.environ", {"SETUK_MAX_TOKENS_PER_RUN": "1000"}, clear=True):
            with patch("version_C.llm.call_agent", must_not_be_called):
                result = self.pipeline.node_draft(state)
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
