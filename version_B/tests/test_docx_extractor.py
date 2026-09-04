import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import extract_docx


DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>이름: 이세비</w:t></w:r></w:p>
    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>표 안의 탐구 결과</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
  </w:body>
</w:document>
"""


def make_docx(path: Path, include_media: bool = False) -> None:
    with zipfile.ZipFile(path, "w") as document:
        document.writestr("word/document.xml", DOCUMENT_XML)
        if include_media:
            document.writestr("word/media/image1.png", b"not-a-real-image")


class DocxExtractorTests(unittest.TestCase):
    def test_extracts_body_and_table_paragraphs_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.docx"
            make_docx(path, include_media=True)
            result = extract_docx.extract_docx(path)
            self.assertEqual(
                ["이름: 이세비", "표 안의 탐구 결과"],
                [item["text"] for item in result["paragraphs"]],
            )
            self.assertEqual(["word/media/image1.png"], result["media_files"])

    def test_cli_returns_utf8_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "보고서.docx"
            make_docx(path)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "extract_docx.py"),
                    str(path),
                    "--json",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload["success"])
            self.assertEqual(2, payload["paragraph_count"])

    def test_rejects_non_docx_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.zip"
            make_docx(path)
            with self.assertRaises(extract_docx.DocxExtractionError):
                extract_docx.extract_docx(path)

    def test_rejects_oversized_document_xml(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.docx"
            make_docx(path)
            with self.assertRaises(extract_docx.DocxExtractionError):
                extract_docx.extract_docx(path, max_xml_bytes=10)


if __name__ == "__main__":
    unittest.main()
