from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from version_C import server


class ServerRoutingTests(unittest.TestCase):
    def setUp(self):
        server.app.testing = True
        self.client = server.app.test_client()
        with server._RUNS_LOCK:
            server._RUNS.clear()

    def test_index_page_loads(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Setuk-AI C버전", response.get_data(as_text=True))

    def test_start_without_student_key_is_rejected(self):
        response = self.client.post("/start", data={"mode": "yaml"})
        self.assertEqual(response.status_code, 400)

    def test_unknown_run_detail_is_404(self):
        response = self.client.get("/runs/does-not-exist")
        self.assertEqual(response.status_code, 404)

    def test_review_before_awaiting_review_is_rejected(self):
        with server._RUNS_LOCK:
            server._RUNS["fixed-id"] = {"student_key": "테스트", "status": "running"}
        response = self.client.post(
            "/runs/fixed-id/review", data={"action": "approve", "edited_text": ""}
        )
        self.assertEqual(response.status_code, 409)

    def test_start_with_missing_docx_ends_in_error_status(self):
        response = self.client.post(
            "/start",
            data={"mode": "docx", "student_key": "없는학생", "report_path": "보고서/없음.docx"},
        )
        self.assertEqual(response.status_code, 302)
        run_id = unquote(response.headers["Location"].rsplit("/", 1)[-1])

        for _ in range(50):
            with server._RUNS_LOCK:
                status = server._RUNS[run_id]["status"]
            if status != "running":
                break
            time.sleep(0.1)

        with server._RUNS_LOCK:
            info = server._RUNS[run_id]
        self.assertEqual(info["status"], "error")
        self.assertIn("DOCX", info["error"])


if __name__ == "__main__":
    unittest.main()
