from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from version_C import db as dbmod

KEY_A = Fernet.generate_key().decode()
KEY_B = Fernet.generate_key().decode()


def raw_column(database: dbmod.Database, table: str, column: str, row_id: str, id_column: str = "id"):
    with database.engine.connect() as conn:
        result = conn.execute(
            text(f"SELECT {column} FROM {table} WHERE {id_column} = :id"), {"id": row_id}
        )
        row = result.first()
        return row[0] if row else None


class EncryptedTextTests(unittest.TestCase):
    def setUp(self):
        self.database = dbmod.Database("sqlite:///:memory:")
        self.database.create_all()

    def tearDown(self):
        self.database.engine.dispose()

    def test_without_a_key_student_key_is_stored_as_plaintext(self):
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("SETUK_DB_ENCRYPTION_KEY", None)
            self.assertFalse(dbmod.encryption_enabled())
            with self.database.session() as session:
                session.add(
                    dbmod.Run(id="r1", student_key="김준수", pseudonym="stu_x", mode="yaml", status="running")
                )
            raw = raw_column(self.database, "runs", "student_key", "r1")
            self.assertEqual(raw, "김준수")
            with self.database.session() as session:
                self.assertEqual(session.get(dbmod.Run, "r1").student_key, "김준수")

    def test_with_a_key_student_key_is_not_stored_in_the_clear(self):
        with patch.dict("os.environ", {"SETUK_DB_ENCRYPTION_KEY": KEY_A}):
            self.assertTrue(dbmod.encryption_enabled())
            with self.database.session() as session:
                session.add(
                    dbmod.Run(id="r2", student_key="김준수", pseudonym="stu_x", mode="yaml", status="running")
                )
            raw = raw_column(self.database, "runs", "student_key", "r2")
            self.assertNotIn("김준수", raw)
            self.assertTrue(raw.startswith(dbmod._ENC_PREFIX))
            with self.database.session() as session:
                self.assertEqual(session.get(dbmod.Run, "r2").student_key, "김준수")

    def test_wrong_key_yields_a_clear_failure_marker_not_a_crash(self):
        with patch.dict("os.environ", {"SETUK_DB_ENCRYPTION_KEY": KEY_A}):
            with self.database.session() as session:
                session.add(
                    dbmod.Run(id="r3", student_key="김준수", pseudonym="stu_x", mode="yaml", status="running")
                )
        with patch.dict("os.environ", {"SETUK_DB_ENCRYPTION_KEY": KEY_B}):
            with self.database.session() as session:
                self.assertEqual(
                    session.get(dbmod.Run, "r3").student_key, dbmod.DECRYPTION_FAILED_MARKER
                )

    def test_key_added_after_the_fact_still_reads_old_plaintext_rows(self):
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("SETUK_DB_ENCRYPTION_KEY", None)
            with self.database.session() as session:
                session.add(
                    dbmod.Run(id="r4", student_key="김준수", pseudonym="stu_x", mode="yaml", status="running")
                )
        with patch.dict("os.environ", {"SETUK_DB_ENCRYPTION_KEY": KEY_A}):
            with self.database.session() as session:
                self.assertEqual(session.get(dbmod.Run, "r4").student_key, "김준수")


class EncryptedJSONTests(unittest.TestCase):
    def setUp(self):
        self.database = dbmod.Database("sqlite:///:memory:")
        self.database.create_all()

    def tearDown(self):
        self.database.engine.dispose()

    def test_mapping_round_trips_encrypted(self):
        with patch.dict("os.environ", {"SETUK_DB_ENCRYPTION_KEY": KEY_A}):
            store = dbmod.DatabasePseudonymStore(self.database)
            store.save("run-1", {"김준수": "[학생1]"})
            raw = raw_column(self.database, "pseudonym_maps", "mapping", "run-1", id_column="run_id")
            self.assertNotIn("김준수", raw)
            self.assertEqual(store.load("run-1"), {"김준수": "[학생1]"})

    def test_wrong_key_returns_empty_mapping_not_a_crash(self):
        with patch.dict("os.environ", {"SETUK_DB_ENCRYPTION_KEY": KEY_A}):
            dbmod.DatabasePseudonymStore(self.database).save("run-2", {"김준수": "[학생1]"})
        with patch.dict("os.environ", {"SETUK_DB_ENCRYPTION_KEY": KEY_B}):
            self.assertEqual(dbmod.DatabasePseudonymStore(self.database).load("run-2"), {})


if __name__ == "__main__":
    unittest.main()
