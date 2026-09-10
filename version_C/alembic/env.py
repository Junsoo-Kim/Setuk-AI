"""Alembic 실행 환경.

접속 URL은 `alembic.ini`가 아니라 `version_C/.env`의 `DATABASE_URL`에서 읽는다.
비밀번호가 든 문자열을 저장소에 남기지 않기 위함이며, 서버(`db.database_url()`)와
같은 값을 보도록 함수를 그대로 재사용한다.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

VERSION_C = Path(__file__).resolve().parents[1]
REPO_ROOT = VERSION_C.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

load_dotenv(VERSION_C / ".env")

from version_C.db import Base, database_url  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", database_url())
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite는 ALTER를 거의 지원하지 않으므로 배치 모드로 재작성한다.
        render_as_batch=database_url().startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
