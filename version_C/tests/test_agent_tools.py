"""파이프라인 에이전트가 MCP 클라이언트로 도구를 쓰는 경로의 테스트.

C버전의 MCP 서버는 원래 외부 IDE(Claude Code·Cline)만을 위한 창구였다. 이 테스트가
고정하는 요구사항은 **파이프라인 내부 에이전트도 같은 MCP 서버를 같은 프로토콜로 쓴다**는
것이다. 그래야 경로 제한·쓰기 차단·감사 로그·호출 상한 같은 정책이 한곳(MCP 서버)에만
있고, 내부와 외부가 같은 정책을 거친다.

- 단계별 최소 권한: 단계마다 허용된 도구만 모델에게 보인다. 실명을 돌려줄 수 있는 도구
  (`extract_docx`·`validate_student_yaml`)와 쓰기 도구(`submit_review`)는 어떤 단계에도 없다.
- 허용 밖 호출은 서버에 닿기 전에 거부되고, 모델이 읽을 수 있는 `is_error` 결과로 돌아간다.
- tool-use 루프는 라운드 상한을 넘으면 도구를 끄고(`tool_choice: none`) 답을 받는다.
- 실제 Anthropic API는 호출하지 않는다. 모델 응답은 스크립트로 흉내 내고, MCP 서버는
  실제 서버를 인메모리 전송으로 붙인다.
"""

from __future__ import annotations

import json
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.memory import MemorySaver

from version_C import agent_tools, llm, pipeline
from version_C.agent_tools import STAGE_TOOLS, McpToolbox, open_toolbox
from version_C.llm import CallMetrics, request_contract

from version_C.tests.test_pipeline import (
    TempProject,
    draft_response,
    start_state,
    structured_facts_response,
)

# 어떤 단계에서도 모델에게 보여서는 안 되는 도구.
FORBIDDEN_FOR_AGENTS = {"submit_review", "extract_docx", "validate_student_yaml"}


class Recorder:
    def __init__(self):
        self.entries: list[tuple[str, dict]] = []

    def __call__(self, tool_name: str, detail: dict) -> None:
        self.entries.append((tool_name, detail))


# --------------------------------------------------------------------------- 가짜 모델


def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def tool_use_block(tool_id: str, name: str, arguments: dict):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=arguments)


def message(content: list, stop_reason: str, input_tokens: int = 10, output_tokens: int = 5):
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class ScriptedClient:
    """`client.messages.create`만 흉내 낸다. 요청 인자를 그대로 기록한다."""

    def __init__(self, responder):
        self._responder = responder
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        # 이후 루프가 messages 리스트를 이어 붙이므로 호출 시점의 모습을 복사해 둔다.
        snapshot = dict(kwargs)
        snapshot["messages"] = list(kwargs.get("messages") or [])
        self.requests.append(snapshot)
        return self._responder(len(self.requests), kwargs)


def scripted(*responses):
    queue = list(responses)

    def responder(_index, _kwargs):
        if not queue:
            raise AssertionError("예상보다 모델이 더 많이 호출되었습니다.")
        return queue.pop(0)

    return ScriptedClient(responder)


def config():
    return llm.LLMConfig(api_key="test-key", model="test-model", max_tokens=1000)


# --------------------------------------------------------------------------- 도구 상자


class StageAllowlistTests(unittest.TestCase):
    def test_generation_stages_have_policy_and_lint_tools(self):
        for stage in ("draft", "evaluate", "fix"):
            self.assertIn("search_school_policy", STAGE_TOOLS[stage], stage)
            self.assertIn("lint_record", STAGE_TOOLS[stage], stage)

    def test_no_stage_exposes_pii_or_write_tools(self):
        for stage, tools in STAGE_TOOLS.items():
            self.assertFalse(FORBIDDEN_FOR_AGENTS & set(tools), stage)

    def test_stage_without_tools_gets_no_toolbox(self):
        self.assertIsNone(open_toolbox("structure"))
        self.assertIsNone(open_toolbox("ingest"))

    def test_tools_can_be_disabled_by_environment(self):
        with patch.dict("os.environ", {"SETUK_AGENT_TOOLS": "0"}):
            self.assertFalse(agent_tools.agent_tools_enabled())
            self.assertIsNone(open_toolbox("draft"))

    def test_tools_are_enabled_by_default(self):
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("SETUK_AGENT_TOOLS", None)
            self.assertTrue(agent_tools.agent_tools_enabled())


class ToolboxOverMcpTests(unittest.TestCase):
    """실제 MCP 서버에 인메모리 전송으로 붙는다(프로토콜 핸드셰이크 포함)."""

    def test_lists_only_tools_allowed_for_the_stage_via_mcp(self):
        with McpToolbox("draft") as toolbox:
            names = {tool["name"] for tool in toolbox.anthropic_tools()}
        self.assertEqual(names, set(STAGE_TOOLS["draft"]))

    def test_tool_definitions_carry_the_server_input_schema(self):
        with McpToolbox("draft") as toolbox:
            tools = {tool["name"]: tool for tool in toolbox.anthropic_tools()}
        lint = tools["lint_record"]
        self.assertIn("description", lint)
        self.assertEqual(lint["input_schema"]["required"], ["text"])

    def test_server_instructions_are_exposed_to_the_agent(self):
        with McpToolbox("draft") as toolbox:
            self.assertIn("search_school_policy", toolbox.instructions)

    def test_allowed_tool_call_goes_through_the_server(self):
        recorder = Recorder()
        with McpToolbox("draft", audit=recorder) as toolbox:
            outcome = toolbox.call("lint_record", {"text": "탐구 과정에서 분석 능력이 드러남."})
        self.assertFalse(outcome.is_error)
        payload = json.loads(outcome.text)
        self.assertIn("passed", payload)
        self.assertEqual([name for name, _ in recorder.entries], ["lint_record"])
        self.assertEqual(recorder.entries[0][1]["outcome"], "ok")

    def test_disallowed_tool_is_refused_before_reaching_the_server(self):
        recorder = Recorder()
        with McpToolbox("draft", audit=recorder) as toolbox:
            for name in sorted(FORBIDDEN_FOR_AGENTS):
                outcome = toolbox.call(name, {"run_id": "x", "decision": "approve"})
                self.assertTrue(outcome.is_error, name)
                self.assertIn(name, outcome.text)
        # 서버 쪽 감사 로그에 아무것도 남지 않았다 = 서버에 요청이 가지 않았다.
        self.assertEqual(recorder.entries, [])

    def test_server_side_tool_error_becomes_a_readable_error_result(self):
        with McpToolbox("draft") as toolbox:
            outcome = toolbox.call("lint_record", {"text": "   "})
        self.assertTrue(outcome.is_error)
        self.assertIn("비어", outcome.text)


# --------------------------------------------------------------------------- 에이전트 루프


class FakeToolbox:
    """MCP 없이 루프만 검증할 때 쓰는 도구 상자."""

    instructions = "도구 안내문"

    def __init__(self, error_tools: set[str] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self._error_tools = error_tools or set()

    def anthropic_tools(self):
        return [
            {
                "name": "lint_record",
                "description": "검사",
                "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
            }
        ]

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name in self._error_tools:
            return agent_tools.ToolOutcome(text=f"{name} 실패", is_error=True)
        return agent_tools.ToolOutcome(text=json.dumps({"passed": True}), is_error=False)


class AgentLoopTests(unittest.TestCase):
    def run_agent(self, client, toolbox):
        with patch.object(llm, "_anthropic_client", lambda _config: client):
            return llm.call_agent("시스템", "사용자 입력", config(), toolbox=toolbox)

    def test_without_toolbox_no_tools_are_sent(self):
        client = scripted(message([text_block("답")], "end_turn"))
        result = self.run_agent(client, None)
        self.assertEqual(result, "답")
        self.assertNotIn("tools", client.requests[0])

    def test_tool_use_is_executed_and_result_returned_with_matching_id(self):
        toolbox = FakeToolbox()
        first = message(
            [text_block("검사해 보겠다."), tool_use_block("tu_1", "lint_record", {"text": "본문"})],
            "tool_use",
        )
        client = scripted(first, message([text_block("최종 답")], "end_turn"))

        result = self.run_agent(client, toolbox)

        self.assertEqual(result, "최종 답")
        self.assertEqual(toolbox.calls, [("lint_record", {"text": "본문"})])
        self.assertEqual(client.requests[0]["tools"][0]["name"], "lint_record")

        follow_up = client.requests[1]["messages"]
        # 직전 assistant 턴은 받은 content를 그대로 이어 붙인다(append-only).
        self.assertEqual(follow_up[1], {"role": "assistant", "content": first.content})
        results = follow_up[2]["content"]
        self.assertEqual(results[0]["type"], "tool_result")
        self.assertEqual(results[0]["tool_use_id"], "tu_1")
        self.assertFalse(results[0].get("is_error", False))

    def test_parallel_tool_calls_return_all_results_in_one_message(self):
        toolbox = FakeToolbox()
        first = message(
            [
                tool_use_block("tu_1", "lint_record", {"text": "가"}),
                tool_use_block("tu_2", "lint_record", {"text": "나"}),
            ],
            "tool_use",
        )
        client = scripted(first, message([text_block("끝")], "end_turn"))
        self.run_agent(client, toolbox)
        results = client.requests[1]["messages"][2]["content"]
        self.assertEqual([item["tool_use_id"] for item in results], ["tu_1", "tu_2"])

    def test_tool_error_is_reported_to_the_model_not_raised(self):
        toolbox = FakeToolbox(error_tools={"lint_record"})
        client = scripted(
            message([tool_use_block("tu_1", "lint_record", {"text": ""})], "tool_use"),
            message([text_block("끝")], "end_turn"),
        )
        self.run_agent(client, toolbox)
        result = client.requests[1]["messages"][2]["content"][0]
        self.assertTrue(result["is_error"])
        self.assertIn("실패", result["content"])

    def test_server_instructions_are_added_to_the_system_prompt(self):
        client = scripted(message([text_block("끝")], "end_turn"))
        self.run_agent(client, FakeToolbox())
        self.assertIn("시스템", client.requests[0]["system"])
        self.assertIn("도구 안내문", client.requests[0]["system"])

    def test_round_limit_forces_a_final_answer_without_tools(self):
        toolbox = FakeToolbox()

        def responder(_index, kwargs):
            if (kwargs.get("tool_choice") or {}).get("type") == "none":
                return message([text_block("도구 없이 낸 답")], "end_turn")
            return message([tool_use_block("tu", "lint_record", {"text": "x"})], "tool_use")

        client = ScriptedClient(responder)
        result = self.run_agent(client, toolbox)

        self.assertEqual(result, "도구 없이 낸 답")
        self.assertEqual(len(toolbox.calls), llm.MAX_TOOL_ROUNDS)
        self.assertEqual(len(client.requests), llm.MAX_TOOL_ROUNDS + 1)
        self.assertEqual(client.requests[-1]["tool_choice"], {"type": "none"})

    def test_usage_is_summed_across_tool_rounds(self):
        client = scripted(
            message([tool_use_block("tu", "lint_record", {"text": "x"})], "tool_use", 100, 20),
            message([text_block("끝")], "end_turn", 150, 30),
        )
        self.run_agent(client, FakeToolbox())
        self.assertEqual(llm._LAST_USAGE, {"input_tokens": 250, "output_tokens": 50})


class ContractMetricsTests(unittest.TestCase):
    def test_tool_calls_are_recorded_in_stage_metrics(self):
        toolbox = FakeToolbox(error_tools=set())
        client = scripted(
            message([tool_use_block("tu", "lint_record", {"text": "x"})], "tool_use"),
            message([text_block(draft_response())], "end_turn"),
        )
        tracker = CallMetrics(stage="draft")
        with patch.object(llm, "_anthropic_client", lambda _config: client):
            request_contract(
                "시스템", "입력", config(), "draft", metrics=tracker, toolbox=toolbox
            )
        detail = tracker.as_detail()
        self.assertEqual(detail["tool_calls"], [{"name": "lint_record", "is_error": False}])

    def test_metrics_have_empty_tool_calls_when_no_tools_used(self):
        self.assertEqual(CallMetrics(stage="draft").as_detail()["tool_calls"], [])


# --------------------------------------------------------------------------- 파이프라인


class PipelineToolUseTests(unittest.TestCase):
    """structure는 도구 없이, draft는 MCP 도구를 쓰는 에이전트로 실행되는지 본다."""

    def run_until_review(self, tmp_root, client, env=None):
        (tmp_root / "학생정보" / "테스트.yaml").write_text("schema_version: 1\n", encoding="utf-8")
        with ExitStack() as stack:
            stack.enter_context(patch.object(pipeline, "ROOT", tmp_root))
            stack.enter_context(patch.object(pipeline, "load_config", config))
            stack.enter_context(patch.object(llm, "_anthropic_client", lambda _c: client))
            if env is not None:
                stack.enter_context(patch.dict("os.environ", env))
            graph = pipeline.build_graph(MemorySaver())
            return graph.invoke(start_state(), config={"configurable": {"thread_id": "t-tools"}})

    def test_draft_agent_consults_policy_through_mcp(self):
        def responder(index, kwargs):
            if index == 1:  # structure
                return message([text_block(structured_facts_response())], "end_turn")
            if index == 2:  # draft: 규정을 먼저 확인한다
                return message(
                    [tool_use_block("tu_p", "search_school_policy", {"query": "수상 실적 기재"})],
                    "tool_use",
                )
            return message([text_block(draft_response())], "end_turn")

        client = ScriptedClient(responder)
        with TempProject() as tmp_root:
            state = self.run_until_review(tmp_root, client)

        self.assertIn("__interrupt__", state)
        self.assertNotIn("tools", client.requests[0])  # structure 단계
        draft_tools = {tool["name"] for tool in client.requests[1]["tools"]}
        self.assertEqual(draft_tools, set(STAGE_TOOLS["draft"]))
        tool_result = client.requests[2]["messages"][-1]["content"][0]
        self.assertEqual(tool_result["tool_use_id"], "tu_p")
        self.assertFalse(tool_result.get("is_error", False))

        draft_metrics = [item for item in state["metrics"] if item["stage"] == "draft"]
        self.assertEqual(
            draft_metrics[0]["tool_calls"], [{"name": "search_school_policy", "is_error": False}]
        )

    def test_disabled_tools_fall_back_to_single_call_drafting(self):
        def responder(index, _kwargs):
            body = structured_facts_response() if index == 1 else draft_response()
            return message([text_block(body)], "end_turn")

        client = ScriptedClient(responder)
        with TempProject() as tmp_root:
            state = self.run_until_review(tmp_root, client, env={"SETUK_AGENT_TOOLS": "0"})

        self.assertIn("__interrupt__", state)
        self.assertEqual(len(client.requests), 2)
        self.assertTrue(all("tools" not in request for request in client.requests))


if __name__ == "__main__":
    unittest.main()
