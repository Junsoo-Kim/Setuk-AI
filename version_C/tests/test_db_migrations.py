"""Alembic 마이그레이션이 빈 DB에서 최신 스키마까지 실제로 올라가는지 검증한다.

지금까지 마이그레이션 파일(`alembic/versions/*.py`)은 여러 개 쌓였지만, 빈 DB에
`alembic upgrade head`를 실제로 실행해 그 결과를 확인하는 테스트는 없었다.
AI가 `db.py`의 ORM 모델에 필드를 추가하면서 그에 대응하는 마이그레이션 파일을
빠뜨리는 실수는 코드 리뷰만으로는 놓치기 쉽다 — 로컬 SQLite는 이미 몇 단계
지난 파일을 갖고 있어서 새 컬럼 없이도 조용히 동작하는 것처럼 보이기 때문이다.
이 테스트는 매번 빈 DB에서 새로 올려서 그 착시를 없앤다.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

VERSION_C = ROOT / "version_C"
ALEMBIC_INI = VERSION_C / "alembic.ini"
ALEMBIC_SCRIPTS = VERSION_C / "alembic"

EXPECTED_TABLES = {"runs", "reviews", "artifacts", "audit_logs", "pseudonym_maps"}

# 마이그레이션이 순서대로 추가해 온 runs 컬럼들. 하나라도 없으면 그 마이그레이션이
# 빠졌거나 실패한 것이다.
EXPECTED_RUNS_COLUMNS = {
    "id",
    "student_key",
    "pseudonym",
    "mode",
    "status",
    "report_path",
    "yaml_path",
    "output_path",
    "draft_text",
    "draft_hash",
    "lint_summary",
    "lint_retry_count",
    "lint_passed",
    "error",
    "warnings",
    "prompt_version",
    "model_name",
    "lease_owner",
    "lease_expires_at",
    "created_at",
    "updated_at",
    "version",
    "policy_index_version",  # 0002
    "policy_year",  # 0002
    "policy_findings",  # 0002
    "needs_input",  # 0003
}


def _alembic_config(sqlite_path: Path) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_SCRIPTS))
    # env.py는 database_url()을 통해 DATABASE_URL 환경변수를 읽으므로, 이 값이
    # 곧 실제로 마이그레이션이 적용되는 대상이다.
    return cfg


class _TempDatabase:
    """실제 파일 기반 SQLite. in-memory는 alembic이 새 커넥션마다 다른 DB를 볼 수 있다."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "migration-test.sqlite3"
        self.url = f"sqlite:///{self.path.as_posix()}"
        self._env_patch = patch.dict("os.environ", {"DATABASE_URL": self.url})
        self._env_patch.start()
        return self

    def __exit__(self, *exc):
        self._env_patch.stop()
        self._tmp.cleanup()


class UpgradeHeadTests(unittest.TestCase):
    def test_upgrade_head_succeeds_on_empty_database(self):
        with _TempDatabase() as db:
            cfg = _alembic_config(db.path)
            command.upgrade(cfg, "head")  # 예외 없이 끝나야 한다.
            self.assertTrue(db.path.exists())

    def test_all_expected_tables_exist_after_upgrade(self):
        with _TempDatabase() as db:
            command.upgrade(_alembic_config(db.path), "head")
            engine = create_engine(db.url)
            try:
                inspector = inspect(engine)
                tables = set(inspector.get_table_names())
            finally:
                engine.dispose()
            missing = EXPECTED_TABLES - tables
            self.assertEqual(set(), missing, f"누락된 테이블: {missing}")

    def test_runs_table_has_every_expected_column(self):
        with _TempDatabase() as db:
            command.upgrade(_alembic_config(db.path), "head")
            engine = create_engine(db.url)
            try:
                inspector = inspect(engine)
                columns = {col["name"] for col in inspector.get_columns("runs")}
            finally:
                engine.dispose()
            missing = EXPECTED_RUNS_COLUMNS - columns
            self.assertEqual(set(), missing, f"runs 테이블에 없는 컬럼: {missing}")

    def test_review_idempotency_constraint_rejects_duplicates(self):
        """0001이 만든 uq_review_idempotency 제약이 실제로 걸리는지 확인한다."""
        with _TempDatabase() as db:
            command.upgrade(_alembic_config(db.path), "head")
            engine = create_engine(db.url)
            try:
                now = datetime.now(timezone.utc)
                run_id = uuid.uuid4().hex
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "INSERT INTO runs (id, student_key, pseudonym, mode, status, "
                            "lint_retry_count, lint_passed, created_at, updated_at, version) "
                            "VALUES (:id, 'k', 'p', 'yaml', 'running', 0, 0, :now, :now, 1)"
                        ),
                        {"id": run_id, "now": now},
                    )

                def _insert_review(idempotency_key: str) -> None:
                    with engine.begin() as conn:
                        conn.execute(
                            text(
                                "INSERT INTO reviews (id, run_id, idempotency_key, decision, "
                                "reviewer, edited, created_at) VALUES "
                                "(:id, :run_id, :key, 'approve', 'teacher', 0, :now)"
                            ),
                            {
                                "id": uuid.uuid4().hex,
                                "run_id": run_id,
                                "key": idempotency_key,
                                "now": now,
                            },
                        )

                _insert_review("approve-abc")
                with self.assertRaises(IntegrityError):
                    _insert_review("approve-abc")
            finally:
                engine.dispose()

    def test_existing_data_survives_later_migrations(self):
        """0001까지만 올린 뒤 데이터를 넣고, head까지 마저 올려도 데이터가 남아 있는지 확인한다.

        0002·0003은 SQLite 배치 모드로 runs 테이블을 재작성하므로, 배치 작업이
        컬럼을 잘못 옮기면 기존 행이 조용히 사라지거나 깨질 수 있다.
        """
        with _TempDatabase() as db:
            cfg = _alembic_config(db.path)
            command.upgrade(cfg, "0001")

            engine = create_engine(db.url)
            run_id = uuid.uuid4().hex
            now = datetime.now(timezone.utc)
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "INSERT INTO runs (id, student_key, pseudonym, mode, status, "
                            "lint_retry_count, lint_passed, created_at, updated_at, version) "
                            "VALUES (:id, '김철수', 'p1', 'yaml', 'done', 0, 1, :now, :now, 1)"
                        ),
                        {"id": run_id, "now": now},
                    )
            finally:
                engine.dispose()

            command.upgrade(cfg, "head")

            engine = create_engine(db.url)
            try:
                with engine.connect() as conn:
                    row = conn.execute(
                        text("SELECT student_key, status FROM runs WHERE id = :id"),
                        {"id": run_id},
                    ).fetchone()
            finally:
                engine.dispose()

            self.assertIsNotNone(row, "0001에서 넣은 행이 이후 마이그레이션에서 사라졌습니다.")
            self.assertEqual(row[0], "김철수")
            self.assertEqual(row[1], "done")


class OrmSchemaSyncTests(unittest.TestCase):
    """`db.py`의 ORM 모델과 마이그레이션이 실제로 만든 스키마가 일치하는지 확인한다.

    ORM 모델에 필드를 추가하고 마이그레이션 파일 추가를 잊는 실수를 이 테스트가 잡는다.
    """

    def test_orm_tables_match_migrated_tables(self):
        from version_C.db import Base

        with _TempDatabase() as db:
            command.upgrade(_alembic_config(db.path), "head")
            engine = create_engine(db.url)
            try:
                migrated_tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
            finally:
                engine.dispose()

        orm_tables = set(Base.metadata.tables.keys())
        self.assertEqual(
            orm_tables,
            migrated_tables,
            "db.py의 ORM 테이블과 마이그레이션이 실제로 만든 테이블이 다릅니다. "
            f"ORM에만 있음: {orm_tables - migrated_tables}, "
            f"마이그레이션에만 있음: {migrated_tables - orm_tables}",
        )

    def test_orm_columns_match_migrated_columns_for_every_table(self):
        from version_C.db import Base

        with _TempDatabase() as db:
            command.upgrade(_alembic_config(db.path), "head")
            engine = create_engine(db.url)
            try:
                inspector = inspect(engine)
                migrated_columns = {
                    table_name: {col["name"] for col in inspector.get_columns(table_name)}
                    for table_name in inspector.get_table_names()
                    if table_name != "alembic_version"
                }
            finally:
                engine.dispose()

        mismatches = []
        for table in Base.metadata.sorted_tables:
            orm_columns = {col.name for col in table.columns}
            db_columns = migrated_columns.get(table.name)
            if db_columns is None:
                mismatches.append(f"{table.name}: 마이그레이션에 테이블 자체가 없음")
                continue
            if orm_columns != db_columns:
                mismatches.append(
                    f"{table.name}: ORM에만 있음={orm_columns - db_columns}, "
                    f"DB에만 있음={db_columns - orm_columns}"
                )
        self.assertEqual([], mismatches, "\n".join(mismatches))


if __name__ == "__main__":
    unittest.main()
