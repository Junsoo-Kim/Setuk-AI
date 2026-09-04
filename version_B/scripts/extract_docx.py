"""Extract ordered paragraph and table text from a DOCX using only the standard library."""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any


WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DOCUMENT_XML = "word/document.xml"
DEFAULT_MAX_XML_BYTES = 10 * 1024 * 1024


class DocxExtractionError(Exception):
    """Raised when a DOCX cannot be safely extracted."""


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def extract_docx(path: Path, max_xml_bytes: int = DEFAULT_MAX_XML_BYTES) -> dict[str, Any]:
    if path.suffix.casefold() != ".docx":
        raise DocxExtractionError("입력 파일의 확장자는 .docx여야 합니다.")
    if not path.is_file():
        raise DocxExtractionError(f"DOCX 파일을 찾을 수 없습니다: {path}")
    if max_xml_bytes <= 0:
        raise DocxExtractionError("max_xml_bytes는 1 이상이어야 합니다.")

    try:
        with zipfile.ZipFile(path) as document:
            try:
                info = document.getinfo(DOCUMENT_XML)
            except KeyError as exc:
                raise DocxExtractionError("유효한 DOCX 본문을 찾을 수 없습니다.") from exc
            if info.file_size > max_xml_bytes:
                raise DocxExtractionError(
                    f"DOCX 본문이 안전 제한을 초과했습니다: {info.file_size}바이트"
                )
            xml_bytes = document.read(info)
            media_files = sorted(
                item.filename
                for item in document.infolist()
                if item.filename.startswith("word/media/") and not item.is_dir()
            )
    except (OSError, zipfile.BadZipFile) as exc:
        raise DocxExtractionError(f"DOCX 압축 구조를 읽을 수 없습니다: {path}") from exc

    upper_xml = xml_bytes[:4096].upper()
    if b"<!DOCTYPE" in upper_xml or b"<!ENTITY" in upper_xml:
        raise DocxExtractionError("DOCTYPE 또는 ENTITY가 포함된 XML은 처리하지 않습니다.")

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise DocxExtractionError("DOCX 본문 XML 문법이 올바르지 않습니다.") from exc

    namespaces = {"w": WORD_NAMESPACE}
    text_tag = f"{{{WORD_NAMESPACE}}}t"
    paragraphs: list[dict[str, Any]] = []
    for paragraph in root.findall(".//w:body//w:p", namespaces):
        text = "".join(node.text or "" for node in paragraph.iter(text_tag)).strip()
        if text:
            paragraphs.append({"index": len(paragraphs) + 1, "text": text})

    if not paragraphs:
        raise DocxExtractionError("DOCX 본문에서 텍스트를 찾을 수 없습니다.")

    return {
        "file": str(path),
        "paragraph_count": len(paragraphs),
        "paragraphs": paragraphs,
        "text": "\n".join(item["text"] for item in paragraphs),
        "media_files": media_files,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DOCX의 문단과 표 텍스트를 추출합니다.")
    parser.add_argument("file", type=Path, help="읽을 DOCX 파일")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--max-xml-bytes", type=int, default=DEFAULT_MAX_XML_BYTES)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = build_parser().parse_args(argv)
    try:
        result = extract_docx(args.file, args.max_xml_bytes)
    except DocxExtractionError as exc:
        if args.json_output:
            print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    if args.json_output:
        print(json.dumps({"success": True, **result}, ensure_ascii=False, indent=2))
    else:
        print(result["text"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
