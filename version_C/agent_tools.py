"""파이프라인 에이전트용 MCP 클라이언트.

`mcp_server.py`는 처음에 외부 IDE(Claude Code·Cline)만을 위한 창구였고, 파이프라인은
규정 검색과 Linter를 파이썬 함수로 직접 불렀다. 그러면 같은 기능에 대한 정책(경로 제한,
쓰기 차단, 감사 로그, 호출 상한)이 두 갈래가 된다.

여기서는 초안·평가·수정 에이전트가 **같은 MCP 서버에 MCP 클라이언트로 붙는다.** 도구
목록은 `tools/list`로 받고, 실행은 `tools/call`로 한다. 정책은 서버 한곳에만 있고 내부와
외부가 같은 경로를 거친다.

## 전송

Messages API의 MCP 커넥터는 원격(URL) 서버만 받는다. 이 서버는 학생 데이터가 있는 로컬
프로세스여야 하므로, 서버 객체를 같은 프로세스에 띄우고 **인메모리 스트림**으로 연결한다.
JSON-RPC 메시지와 핸드셰이크는 stdio와 같고, 하위 프로세스를 띄우는 비용만 없다.

파이프라인 노드는 동기 함수이고 MCP SDK는 비동기다. anyio blocking portal로 별도 스레드에
이벤트 루프를 하나 두고, 이 클래스는 그 위의 동기 파사드다.

## 단계별 최소 권한

`STAGE_TOOLS`에 없는 도구는 모델에게 보이지 않고, 모델이 이름을 지어내 불러도 서버에 닿기
전에 거부된다. 실명이 들어 있는 원본을 돌려주는 도구(`extract_docx`·`validate_student_yaml`)는
가명화를 우회하므로 어떤 단계에도 주지 않는다. 쓰기 도구(`submit_review`)도 마찬가지다 —
교사 승인을 대신할 수 있는 경로는 웹 화면 하나뿐이다.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable

# 단계 → 그 단계 에이전트가 쓸 수 있는 도구. 여기 없는 단계는 도구 없이 한 번 호출한다.
# 인프로세스 서버는 요청마다 INFO 로그를 남긴다. 단계마다 수십 줄이 쌓이므로 경고만 남긴다.
logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)

STAGE_TOOLS: dict[str, tuple[str, ...]] = {
    "draft": ("search_school_policy", "lint_record"),
    "evaluate": ("search_school_policy", "lint_record"),
    "fix": ("lint_record", "search_school_policy"),
}


def agent_tools_enabled() -> bool:
    """기본값은 켜짐. `SETUK_AGENT_TOOLS=0`이면 도구 없이 단일 호출로 돌아간다."""
    raw = os.environ.get("SETUK_AGENT_TOOLS", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class ToolOutcome:
    """모델에게 돌려줄 도구 결과. 실패도 예외가 아니라 `is_error`로 전달한다."""

    text: str
    is_error: bool


class McpToolbox:
    """한 단계 동안 열려 있는 MCP 클라이언트 세션.

    세션은 처음 쓸 때 연다. 도구를 한 번도 쓰지 않는 호출(테스트의 FakeAgent 등)은
    서버를 띄우지 않는다.
    """

    def __init__(self, stage: str, *, audit: Callable[[str, dict[str, Any]], None] | None = None):
        self.stage = stage
        self.allowed = frozenset(STAGE_TOOLS.get(stage, ()))
        self._audit = audit
        self._stack: ExitStack | None = None
        self._portal = None
        self._session = None
        self._tools: list[dict[str, Any]] | None = None
        self._instructions = ""

    # ------------------------------------------------------------------ 수명

    def __enter__(self) -> "McpToolbox":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._stack is not None:
            stack, self._stack = self._stack, None
            self._session = self._portal = None
            stack.close()

    def _ensure_open(self) -> None:
        if self._session is not None:
            return
        from anyio.from_thread import start_blocking_portal

        from .mcp_server import build_server
        from .mcp_tools import ToolContext

        context = ToolContext(
            session_id=f"agent-{self.stage}-{uuid.uuid4().hex[:8]}", audit=self._audit
        )
        stack = ExitStack()
        try:
            portal = stack.enter_context(start_blocking_portal())
            session, init = stack.enter_context(
                portal.wrap_async_context_manager(_connect_in_memory(build_server(context)))
            )
            self._instructions = init.instructions or ""
            listed = portal.call(session.list_tools)
        except BaseException:
            stack.close()
            raise
        self._stack, self._portal, self._session = stack, portal, session
        self._tools = [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
            }
            for tool in listed.tools
            if tool.name in self.allowed
        ]

    # ------------------------------------------------------------------ 모델 쪽 인터페이스

    @property
    def instructions(self) -> str:
        """MCP initialize 결과의 서버 안내문. 에이전트 시스템 프롬프트에 덧붙는다."""
        self._ensure_open()
        return self._instructions

    def anthropic_tools(self) -> list[dict[str, Any]]:
        """MCP `tools/list` 결과 중 이 단계에 허용된 것만 Messages API 형식으로 바꾼다."""
        self._ensure_open()
        return list(self._tools or [])

    def call(self, name: str, arguments: dict[str, Any] | None) -> ToolOutcome:
        if name not in self.allowed:
            return ToolOutcome(
                text=(
                    f"{name}은(는) 이 단계({self.stage})에서 쓸 수 없는 도구입니다. "
                    f"사용 가능한 도구: {', '.join(sorted(self.allowed)) or '없음'}"
                ),
                is_error=True,
            )
        self._ensure_open()
        result = self._portal.call(self._session.call_tool, name, arguments or {})
        if result.structuredContent is not None and not result.isError:
            text = json.dumps(result.structuredContent, ensure_ascii=False, default=str)
        else:
            text = "\n".join(
                getattr(block, "text", "") for block in result.content if getattr(block, "text", "")
            )
        return ToolOutcome(text=text or "(빈 결과)", is_error=bool(result.isError))


@asynccontextmanager
async def _connect_in_memory(server, **client_kwargs):
    """서버와 클라이언트 세션을 메모리 스트림으로 잇고 initialize 결과까지 돌려준다.

    SDK의 `create_connected_server_and_client_session`과 같은 구성이지만, 그 헬퍼는
    initialize 결과(서버 instructions)를 버리므로 직접 연결한다.
    """
    import anyio
    from mcp import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                server.run, server_read, server_write, server.create_initialization_options()
            )
            try:
                async with ClientSession(client_read, client_write, **client_kwargs) as session:
                    init = await session.initialize()
                    yield session, init
            finally:
                tg.cancel_scope.cancel()


def open_toolbox(
    stage: str, *, audit: Callable[[str, dict[str, Any]], None] | None = None
) -> McpToolbox | None:
    """이 단계에 줄 도구 상자. 도구가 꺼져 있거나 허용 도구가 없으면 None."""
    if not agent_tools_enabled() or not STAGE_TOOLS.get(stage):
        return None
    return McpToolbox(stage, audit=audit)
