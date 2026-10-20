"""MCP elicitation으로 교사 검토(Human-in-the-Loop)를 받는 경로의 테스트.

`submit_review`는 모델이 `decision` 인자를 채워 부르는 도구라, MCP 클라이언트가 도구를
자동 승인하도록 설정되면 모델이 교사 대신 승인할 수 있다. 그래서 기본 차단해 두었다.

`request_teacher_review`는 구조가 다르다. 도구 인자에 **결정이 없다** — 모델은 검토를
요청만 할 수 있고, 승인·반려는 MCP elicitation으로 클라이언트가 사람에게 직접 묻는다.
이 테스트가 고정하는 요구사항:

- 모델이 결정을 넘길 통로가 없다(입력 스키마는 run_id 하나).
- elicitation을 지원하지 않는 클라이언트는 거부한다(fail-closed, 결정을 추측하지 않는다).
- 검토 대기 상태가 아니거나, 대화창이 열려 있는 사이 초안이 바뀌면 기록하지 않는다.
- 반려에는 사유가 필요하다. 거절·취소는 아무것도 기록하지 않는다.
- 기록은 웹 화면과 같은 Review 행 + 감사 로그로 남고, 채널이 구분된다.
- 그래프 재개는 웹 서버가 한다: MCP는 run을 `review_submitted`로 넘기고, 웹 서버의
  `claim_submitted_reviews`가 한 번만 집어 간다.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import anyio

from version_C import agent_tools, mcp_tools, privacy
from version_C.db import (
    STATUS_AWAITING_REVIEW,
    STATUS_DONE,
    STATUS_REVIEW_SUBMITTED,
    STATUS_RUNNING,
    AuditLog,
    Database,
    Review,
    Run,
    claim_submitted_reviews,
)
from version_C.mcp_tools import ToolContext, ToolError, call_tool, describe_tools
from version_C.review_elicitation import ELICITATION_SCHEMA, request_teacher_review

DRAFT = "탐구 과정에서 자료를 비교하며 분석 능력이 드러남."


def make_db(case: unittest.TestCase) -> Database:
    """파일 DB. 도구는 스레드에서 돌고, :memory:는 스레드마다 다른 DB가 된다."""
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    case.addCleanup(tmp.cleanup)
    db = Database(f"sqlite:///{(Path(tmp.name) / 'review.sqlite3').as_posix()}")
    db.create_all()
    case.addCleanup(db.engine.dispose)
    return db


def add_run(db: Database, run_id: str = "run-1", **overrides) -> str:
    defaults = dict(
        id=run_id,
        student_key="테스트",
        pseudonym=privacy.pseudonym_for("테스트"),
        mode="yaml",
        status=STATUS_AWAITING_REVIEW,
        draft_text=DRAFT,
        draft_hash=privacy.content_hash(DRAFT),
        prompt_version="agent-test",
    )
    defaults.update(overrides)
    with db.session() as session:
        session.add(Run(**defaults))
    return run_id


class FakeElicit:
    """사람의 응답을 흉내 낸다. 받은 요청을 기록한다."""

    def __init__(self, action: str = "accept", content: dict | None = None, before_answer=None):
        self.action = action
        self.content = content
        self.before_answer = before_answer
        self.requests: list[tuple[str, dict]] = []

    async def __call__(self, message: str, schema: dict):
        self.requests.append((message, schema))
        if self.before_answer is not None:
            self.before_answer()
        return SimpleNamespace(action=self.action, content=self.content)


def run_review(db, elicit, run_id="run-1", supports=True):
    async def go():
        return await request_teacher_review(
            run_id,
            database=db,
            elicit=elicit,
            supports_elicitation=supports,
            client_name="test-client",
        )

    return anyio.run(go)


def reviews(db) -> list[Review]:
    with db.session() as session:
        return list(session.query(Review).all())


def run_status(db, run_id="run-1") -> str:
    with db.session() as session:
        return session.get(Run, run_id).status


def audit_actions(db) -> list[tuple[str, str]]:
    with db.session() as session:
        return [(log.action, log.actor) for log in session.query(AuditLog).order_by(AuditLog.created_at)]


# --------------------------------------------------------------------------- 도구 정의


class ToolDefinitionTests(unittest.TestCase):
    def tool(self):
        return next(t for t in describe_tools() if t["name"] == "request_teacher_review")

    def test_model_cannot_pass_a_decision(self):
        schema = self.tool()["inputSchema"]
        self.assertEqual(set(schema["properties"]), {"run_id"})
        self.assertFalse(schema["additionalProperties"])

    def test_tool_is_marked_as_not_read_only(self):
        self.assertFalse(self.tool()["annotations"]["readOnlyHint"])

    def test_pipeline_agents_never_get_the_review_tool(self):
        for tools in agent_tools.STAGE_TOOLS.values():
            self.assertNotIn("request_teacher_review", tools)

    def test_plain_call_path_without_a_human_channel_is_refused(self):
        with self.assertRaises(ToolError):
            call_tool("request_teacher_review", {"run_id": "x"}, ToolContext(session_id="s"))

    def test_elicitation_schema_asks_only_for_decision_and_reason(self):
        self.assertEqual(set(ELICITATION_SCHEMA["properties"]), {"decision", "reason"})
        self.assertEqual(ELICITATION_SCHEMA["required"], ["decision"])
        self.assertEqual(ELICITATION_SCHEMA["properties"]["decision"]["enum"], ["approve", "reject"])


# --------------------------------------------------------------------------- 검토 흐름


class RefusalTests(unittest.TestCase):
    def test_client_without_elicitation_is_refused_before_asking(self):
        db = make_db(self)
        add_run(db)
        elicit = FakeElicit(content={"decision": "approve"})
        with self.assertRaises(ToolError) as ctx:
            run_review(db, elicit, supports=False)
        self.assertIn("웹", str(ctx.exception))
        self.assertEqual(elicit.requests, [])
        self.assertEqual(reviews(db), [])

    def test_unknown_run_is_refused(self):
        db = make_db(self)
        with self.assertRaises(ToolError):
            run_review(db, FakeElicit(content={"decision": "approve"}), run_id="nope")

    def test_run_not_awaiting_review_is_refused_before_asking(self):
        db = make_db(self)
        add_run(db, status=STATUS_DONE)
        elicit = FakeElicit(content={"decision": "approve"})
        with self.assertRaises(ToolError):
            run_review(db, elicit)
        self.assertEqual(elicit.requests, [])


class DecisionTests(unittest.TestCase):
    def test_teacher_sees_the_draft_in_the_dialog(self):
        db = make_db(self)
        add_run(db)
        elicit = FakeElicit(content={"decision": "approve"})
        run_review(db, elicit)
        message, schema = elicit.requests[0]
        self.assertIn(DRAFT, message)
        self.assertEqual(schema, ELICITATION_SCHEMA)

    def test_approval_is_recorded_like_the_web_screen_and_handed_to_the_server(self):
        db = make_db(self)
        add_run(db)
        result = run_review(db, FakeElicit(content={"decision": "approve"}))

        self.assertTrue(result["recorded"])
        self.assertEqual(result["decision"], "approve")
        # 도구 결과(=모델이 보는 것)에는 초안 본문을 싣지 않는다.
        self.assertNotIn(DRAFT, str(result))

        [entry] = reviews(db)
        self.assertEqual(entry.decision, "approve")
        self.assertTrue(entry.reviewer.startswith("mcp-elicitation"))
        self.assertEqual(entry.draft_hash, privacy.content_hash(DRAFT))
        self.assertEqual(entry.approved_hash, privacy.content_hash(DRAFT))
        self.assertIsNotNone(entry.approved_at)
        self.assertEqual(run_status(db), STATUS_REVIEW_SUBMITTED)
        self.assertIn(("review.approve", "mcp-elicitation"), audit_actions(db))

    def test_rejection_with_reason_is_recorded(self):
        db = make_db(self)
        add_run(db)
        result = run_review(db, FakeElicit(content={"decision": "reject", "reason": "탐구 동기 보강"}))
        self.assertTrue(result["recorded"])
        [entry] = reviews(db)
        self.assertEqual((entry.decision, entry.reason), ("reject", "탐구 동기 보강"))
        self.assertIsNone(entry.approved_at)
        self.assertEqual(run_status(db), STATUS_REVIEW_SUBMITTED)

    def test_rejection_without_reason_is_not_recorded(self):
        db = make_db(self)
        add_run(db)
        result = run_review(db, FakeElicit(content={"decision": "reject", "reason": "  "}))
        self.assertFalse(result["recorded"])
        self.assertEqual(reviews(db), [])
        self.assertEqual(run_status(db), STATUS_AWAITING_REVIEW)

    def test_invalid_decision_value_is_not_recorded(self):
        db = make_db(self)
        add_run(db)
        result = run_review(db, FakeElicit(content={"decision": "yes"}))
        self.assertFalse(result["recorded"])
        self.assertEqual(reviews(db), [])

    def test_decline_and_cancel_record_nothing_but_are_audited(self):
        for action in ("decline", "cancel"):
            with self.subTest(action=action):
                db = make_db(self)
                add_run(db)
                result = run_review(db, FakeElicit(action=action))
                self.assertFalse(result["recorded"])
                self.assertEqual(result["outcome"], action)
                self.assertEqual(reviews(db), [])
                self.assertEqual(run_status(db), STATUS_AWAITING_REVIEW)
                self.assertIn((f"review.elicitation_{action}", "mcp-elicitation"), audit_actions(db))

    def test_draft_changed_while_dialog_was_open_is_refused(self):
        db = make_db(self)
        add_run(db)

        def regenerate():
            with db.session() as session:
                run = session.get(Run, "run-1")
                run.draft_text = "다시 쓴 초안."
                run.draft_hash = privacy.content_hash("다시 쓴 초안.")

        with self.assertRaises(ToolError) as ctx:
            run_review(db, FakeElicit(content={"decision": "approve"}, before_answer=regenerate))
        self.assertIn("바뀌", str(ctx.exception))
        self.assertEqual(reviews(db), [])

    def test_status_changed_while_dialog_was_open_is_refused(self):
        db = make_db(self)
        add_run(db)

        def approved_on_web():
            with db.session() as session:
                session.get(Run, "run-1").status = STATUS_RUNNING

        with self.assertRaises(ToolError):
            run_review(db, FakeElicit(content={"decision": "approve"}, before_answer=approved_on_web))
        self.assertEqual(reviews(db), [])


# --------------------------------------------------------------------------- 웹 서버 인계


class ClaimSubmittedReviewTests(unittest.TestCase):
    def test_approved_review_is_claimed_once_with_resume_payload(self):
        db = make_db(self)
        add_run(db)
        run_review(db, FakeElicit(content={"decision": "approve"}))

        claimed = claim_submitted_reviews(db, "worker-a")
        self.assertEqual(claimed, [("run-1", {"approved": True, "edited_text": ""})])
        self.assertEqual(run_status(db), STATUS_RUNNING)
        with db.session() as session:
            self.assertEqual(session.get(Run, "run-1").lease_owner, "worker-a")

        # 두 번째 워커(또는 다음 주기)는 같은 결정을 다시 집지 않는다.
        self.assertEqual(claim_submitted_reviews(db, "worker-b"), [])

    def test_rejected_review_resumes_with_reason(self):
        db = make_db(self)
        add_run(db)
        run_review(db, FakeElicit(content={"decision": "reject", "reason": "근거 보강"}))
        self.assertEqual(
            claim_submitted_reviews(db, "worker-a"),
            [("run-1", {"approved": False, "reason": "근거 보강"})],
        )

    def test_runs_still_awaiting_review_are_not_claimed(self):
        db = make_db(self)
        add_run(db)
        self.assertEqual(claim_submitted_reviews(db, "worker-a"), [])
        self.assertEqual(run_status(db), STATUS_AWAITING_REVIEW)


# --------------------------------------------------------------------------- MCP 프로토콜


class ElicitationOverMcpTests(unittest.TestCase):
    """실제 MCP 서버와 클라이언트 세션 사이에서 elicitation 왕복을 확인한다."""

    def call(self, db, elicitation_callback=None):
        from mcp.types import ElicitResult

        from version_C.agent_tools import _connect_in_memory
        from version_C.mcp_server import build_server

        seen: list = []

        async def callback(context, params):
            seen.append(params)
            return ElicitResult(action="accept", content={"decision": "approve"})

        kwargs = {}
        if elicitation_callback == "approve":
            kwargs["elicitation_callback"] = callback

        async def go():
            context = ToolContext(session_id="elicit-test")
            async with _connect_in_memory(build_server(context), **kwargs) as (session, _init):
                return await session.call_tool("request_teacher_review", {"run_id": "run-1"})

        with patch.object(mcp_tools, "_DB", db):
            return anyio.run(go), seen

    def test_round_trip_records_the_human_decision(self):
        db = make_db(self)
        add_run(db)
        result, seen = self.call(db, elicitation_callback="approve")
        self.assertFalse(result.isError)
        self.assertTrue(result.structuredContent["recorded"])
        self.assertEqual(len(seen), 1)
        self.assertIn(DRAFT, seen[0].message)
        self.assertEqual(run_status(db), STATUS_REVIEW_SUBMITTED)

    def test_client_without_elicitation_gets_a_readable_error(self):
        db = make_db(self)
        add_run(db)
        result, _ = self.call(db)
        self.assertTrue(result.isError)
        self.assertIn("웹", result.content[0].text)
        self.assertEqual(run_status(db), STATUS_AWAITING_REVIEW)


if __name__ == "__main__":
    unittest.main()
