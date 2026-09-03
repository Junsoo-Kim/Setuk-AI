"""세특 결과물의 NEIS 입력 제약을 검사하는 표준 라이브러리 기반 Linter."""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_RULES_PATH = Path(__file__).with_name("rules.json")


class LinterError(Exception):
    """입력 또는 설정 문제처럼 검사를 수행할 수 없는 오류."""


def configure_stdio() -> None:
    """Windows 포터블 Python에서도 한글과 진단 대상 문자를 UTF-8로 출력한다."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


@dataclass(frozen=True)
class Diagnostic:
    severity: str
    code: str
    message: str
    line: int | None = None
    column: int | None = None


@dataclass(frozen=True)
class LintResult:
    file: str
    byte_count: int
    target_min_bytes: int
    max_bytes: int
    diagnostics: tuple[Diagnostic, ...]

    @property
    def errors(self) -> int:
        return sum(item.severity == "error" for item in self.diagnostics)

    @property
    def warnings(self) -> int:
        return sum(item.severity == "warning" for item in self.diagnostics)

    @property
    def passed(self) -> bool:
        return self.errors == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "passed": self.passed,
            "byte_count": self.byte_count,
            "target_min_bytes": self.target_min_bytes,
            "max_bytes": self.max_bytes,
            "errors": self.errors,
            "warnings": self.warnings,
            "diagnostics": [asdict(item) for item in self.diagnostics],
        }


def load_rules(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise LinterError(f"규칙 파일을 읽을 수 없습니다: {path} ({exc})") from exc

    try:
        rules = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LinterError(
            f"규칙 파일의 JSON 문법이 잘못되었습니다: {path}:{exc.lineno}:{exc.colno}"
        ) from exc

    if not isinstance(rules, dict):
        raise LinterError("규칙 파일의 최상위 값은 객체여야 합니다.")
    if rules.get("schema_version") != 1:
        raise LinterError("지원하지 않는 rules.json schema_version입니다.")

    max_bytes = rules.get("max_bytes")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise LinterError("max_bytes는 1 이상의 정수여야 합니다.")

    target_min_bytes = rules.get("target_min_bytes", 0)
    if (
        not isinstance(target_min_bytes, int)
        or isinstance(target_min_bytes, bool)
        or target_min_bytes < 0
        or target_min_bytes > max_bytes
    ):
        raise LinterError("target_min_bytes는 0 이상 max_bytes 이하의 정수여야 합니다.")

    byte_rules = rules.get("byte_count")
    if not isinstance(byte_rules, dict):
        raise LinterError("byte_count 설정이 필요합니다.")
    for key in ("ascii", "non_ascii", "line_break"):
        value = byte_rules.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise LinterError(f"byte_count.{key}는 0 이상의 정수여야 합니다.")

    for key in ("allowed_punctuation", "allowed_symbols"):
        if not isinstance(rules.get(key), str):
            raise LinterError(f"{key}는 문자열이어야 합니다.")

    forbidden_terms = rules.get("forbidden_terms")
    if not isinstance(forbidden_terms, list):
        raise LinterError("forbidden_terms는 배열이어야 합니다.")
    for index, item in enumerate(forbidden_terms):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("term"), str)
            or not item["term"].strip()
            or not isinstance(item.get("reason"), str)
        ):
            raise LinterError(f"forbidden_terms[{index}]의 term과 reason을 확인하세요.")

    return rules


def read_document(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise LinterError(f"입력 파일은 UTF-8이어야 합니다: {path}") from exc
    except OSError as exc:
        raise LinterError(f"입력 파일을 읽을 수 없습니다: {path} ({exc})") from exc


def neis_byte_count(text: str, byte_rules: dict[str, int]) -> int:
    """설정된 NEIS 방식으로 분량을 계산한다. CRLF와 LF는 한 줄바꿈으로 센다."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    total = 0
    for character in normalized:
        if character == "\n":
            total += byte_rules["line_break"]
        elif ord(character) <= 0x7F:
            total += byte_rules["ascii"]
        else:
            total += byte_rules["non_ascii"]
    return total


def location(text: str, offset: int) -> tuple[int, int]:
    line = text.count("\n", 0, offset) + 1
    last_break = text.rfind("\n", 0, offset)
    column = offset + 1 if last_break < 0 else offset - last_break
    return line, column


def lint_text(text: str, source: str, rules: dict[str, Any]) -> LintResult:
    diagnostics: list[Diagnostic] = []
    count = neis_byte_count(text, rules["byte_count"])
    target_min_bytes = rules.get("target_min_bytes", 0)
    max_bytes = rules["max_bytes"]

    if not text.strip():
        diagnostics.append(Diagnostic("error", "EMPTY_CONTENT", "세특 본문이 비어 있습니다."))
    if count > max_bytes:
        diagnostics.append(
            Diagnostic(
                "error",
                "BYTE_LIMIT",
                f"NEIS 기준 분량이 {count}바이트로 최대 {max_bytes}바이트를 {count - max_bytes}바이트 초과했습니다.",
            )
        )
    elif text.strip() and target_min_bytes and count < target_min_bytes:
        diagnostics.append(
            Diagnostic(
                "warning",
                "BELOW_TARGET_LENGTH",
                f"NEIS 기준 분량이 {count}바이트로 권장 하한 {target_min_bytes}바이트보다 {target_min_bytes - count}바이트 부족합니다.",
            )
        )

    folded_text = text.casefold()
    for item in rules["forbidden_terms"]:
        term = item["term"]
        start = folded_text.find(term.casefold())
        if start >= 0:
            line, column = location(text, start)
            diagnostics.append(
                Diagnostic(
                    "error",
                    "FORBIDDEN_TERM",
                    f"금칙어 '{term}'이(가) 포함되어 있습니다. {item['reason']}",
                    line,
                    column,
                )
            )

    allowed_punctuation = set(rules["allowed_punctuation"])
    allowed_symbols = set(rules["allowed_symbols"])
    reported_characters: set[str] = set()
    for offset, character in enumerate(text):
        if character in "\r\n" or character == " ":
            continue
        if character == "\t":
            line, column = location(text, offset)
            diagnostics.append(
                Diagnostic("error", "TAB_CHARACTER", "탭 문자는 사용할 수 없습니다.", line, column)
            )
            continue

        category = unicodedata.category(character)
        if category[0] in {"L", "N", "M"}:
            continue
        if category[0] == "P" and character in allowed_punctuation:
            continue
        if category[0] == "S" and character in allowed_symbols:
            continue
        if character in reported_characters:
            continue

        reported_characters.add(character)
        line, column = location(text, offset)
        name = unicodedata.name(character, "이름 없음")
        diagnostics.append(
            Diagnostic(
                "error",
                "UNSUPPORTED_CHARACTER",
                f"허용되지 않은 문자 '{character}'(U+{ord(character):04X}, {name})가 포함되어 있습니다.",
                line,
                column,
            )
        )

    diagnostics.sort(
        key=lambda item: (
            0 if item.severity == "error" else 1,
            item.line if item.line is not None else 0,
            item.column if item.column is not None else 0,
            item.code,
        )
    )
    return LintResult(source, count, target_min_bytes, max_bytes, tuple(diagnostics))


def format_text(result: LintResult) -> str:
    status = "통과" if result.passed else "실패"
    lines = [
        f"검사 결과: {status} (오류 {result.errors}건, 경고 {result.warnings}건)",
        f"분량: {result.byte_count}바이트 (권장 {result.target_min_bytes}~{result.max_bytes}바이트)",
    ]
    for item in result.diagnostics:
        position = ""
        if item.line is not None:
            position = f" {item.line}:{item.column}"
        lines.append(f"[{item.severity.upper()}] {item.code}{position} - {item.message}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="세특 마크다운 파일의 NEIS 입력 제약을 검사합니다.")
    parser.add_argument("file", type=Path, help="검사할 UTF-8 마크다운 파일")
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES_PATH, help="규칙 JSON 파일")
    parser.add_argument("--max-bytes", type=int, help="rules.json의 최대 바이트를 일시적으로 재정의")
    parser.add_argument("--json", action="store_true", dest="json_output", help="결과를 JSON으로 출력")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    args = build_parser().parse_args(argv)
    try:
        rules = load_rules(args.rules)
        if args.max_bytes is not None:
            if args.max_bytes <= 0:
                raise LinterError("--max-bytes는 1 이상의 정수여야 합니다.")
            rules = {
                **rules,
                "max_bytes": args.max_bytes,
                "target_min_bytes": min(rules.get("target_min_bytes", 0), args.max_bytes),
            }
        text = read_document(args.file)
        result = lint_text(text, str(args.file), rules)
    except LinterError as exc:
        if args.json_output:
            print(json.dumps({"passed": False, "fatal_error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"[FATAL] {exc}", file=sys.stderr)
        return 2

    if args.json_output:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_text(result))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
