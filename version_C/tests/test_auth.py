from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from version_C import auth
from version_C.db import Database, Teacher


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.db = Database("sqlite:///:memory:")
        self.db.create_all()

    def tearDown(self):
        self.db.engine.dispose()

    def test_create_teacher_hashes_the_password(self):
        auth.create_teacher(self.db, "kim", "password123", role="teacher")
        with self.db.session() as session:
            teacher = session.get(Teacher, "kim")
            self.assertNotEqual(teacher.password_hash, "password123")

    def test_creating_a_duplicate_username_is_rejected(self):
        auth.create_teacher(self.db, "kim", "password123")
        with self.assertRaises(ValueError):
            auth.create_teacher(self.db, "kim", "different-password")

    def test_verify_login_succeeds_with_the_right_password(self):
        auth.create_teacher(self.db, "kim", "password123", role="admin")
        self.assertEqual(auth.verify_login(self.db, "kim", "password123"), "admin")

    def test_verify_login_fails_with_the_wrong_password(self):
        auth.create_teacher(self.db, "kim", "password123")
        self.assertIsNone(auth.verify_login(self.db, "kim", "wrong-password"))

    def test_verify_login_fails_for_an_unknown_user(self):
        self.assertIsNone(auth.verify_login(self.db, "nobody", "anything"))


if __name__ == "__main__":
    unittest.main()
