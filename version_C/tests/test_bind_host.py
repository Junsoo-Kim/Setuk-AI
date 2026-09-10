from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from version_C.run_server import resolve_bind_host


class BindHostTests(unittest.TestCase):
    def test_default_is_loopback(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(resolve_bind_host(), "127.0.0.1")

    def test_explicit_loopback_variants_are_allowed(self):
        for host in ("127.0.0.1", "::1", "localhost"):
            with patch.dict("os.environ", {"SETUK_BIND_HOST": host}, clear=True):
                self.assertEqual(resolve_bind_host(), host)

    def test_non_loopback_is_rejected_by_default(self):
        with patch.dict("os.environ", {"SETUK_BIND_HOST": "0.0.0.0"}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                resolve_bind_host()
        self.assertIn("0.0.0.0", str(ctx.exception))

    def test_non_loopback_allowed_with_explicit_opt_in(self):
        with patch.dict(
            "os.environ",
            {"SETUK_BIND_HOST": "0.0.0.0", "SETUK_ALLOW_NON_LOOPBACK": "1"},
            clear=True,
        ):
            self.assertEqual(resolve_bind_host(), "0.0.0.0")

    def test_opt_in_flag_alone_does_not_change_the_default_host(self):
        with patch.dict("os.environ", {"SETUK_ALLOW_NON_LOOPBACK": "1"}, clear=True):
            self.assertEqual(resolve_bind_host(), "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
