"""LangGraph 체크포인터 선택.

`DATABASE_URL`이 PostgreSQL이면 `PostgresSaver`, 아니면 로컬 SQLite 파일을 쓴다.
LangGraph 공식 안내도 운영 환경에서는 DB 기반 checkpointer를 권한다. run 메타데이터
(`db.py`)와 같은 데이터베이스를 가리키게 해 두 저장소가 어긋나지 않게 한다.

두 saver 모두 컨텍스트 매니저다. 서버 수명 동안 열어 두어야 하므로 `ExitStack`으로
잡아 두고 프로세스 종료 시 닫는다.
"""

from __future__ import annotations

import os
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from .db import checkpointer_dsn, database_url

DEFAULT_SQLITE_CHECKPOINT = Path(__file__).resolve().parent / "run_state_c.sqlite3"


def sqlite_checkpoint_path() -> Path:
    """SQLite 체크포인트 파일 경로. 테스트와 다중 인스턴스를 위해 덮어쓸 수 있다."""
    configured = os.environ.get("SETUK_CHECKPOINT_PATH", "").strip()
    return Path(configured) if configured else DEFAULT_SQLITE_CHECKPOINT


def open_checkpointer(stack: ExitStack, url: str | None = None) -> tuple[Any, str]:
    """(checkpointer, 사람이 읽을 수 있는 백엔드 이름)을 돌려준다."""
    target = url or database_url()
    dsn = checkpointer_dsn(target)

    if dsn is not None:
        from langgraph.checkpoint.postgres import PostgresSaver

        saver = stack.enter_context(PostgresSaver.from_conn_string(dsn))
        saver.setup()
        return saver, "postgres"

    from langgraph.checkpoint.sqlite import SqliteSaver

    path = sqlite_checkpoint_path()
    saver = stack.enter_context(SqliteSaver.from_conn_string(str(path)))
    return saver, f"sqlite({path.name})"
