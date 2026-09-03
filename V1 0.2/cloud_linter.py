"""
cloud_linter.py
================
Setuk-Harness V1 - 세특 초안 기계적 검증 스크립트 (Claude Analysis Tool 실행용)

파이프라인에서의 위치
----------------------
    전처리 -> 생성(02) -> [ 정적 검증(본 스크립트) -> 검증(03) -> 평가(04) ] 최대 2회 반복

    structural_status == "FAIL" 이면 03/04를 거치지 않고 바로 02(재작성 모드)로
    되돌아간다. 03(검증 모듈)는 "review 항목의 문맥 판단"만 전담하며, 길이/금칙어
    같은 구조적 문제를 고치는 별도 모듈은 없다 — 그 수정도 02의 재작성 모드가 맡는다.

역할 분리 원칙
--------------
이 스크립트는 "기계적으로 100% 확실한 것"만 자동 판정한다(block 위반, 길이 초과/미달).
문맥 판단이 필요한 항목(review, warning: 예를 들어 '대회'라는 단어가 실제 3항-나 위반인지,
정상적인 수업 활동 서술인지)은 스크립트가 임의로 판정하지 않고, 매칭된 위치와 문맥만
구조화해서 03_verification.md(검증 모듈, LLM)에게 넘긴다. 검증 모듈이 이 review 목록을
하나하나 검토해서 "실제 위반" vs "정상 서술"을 문맥으로 판단하고, 필요 시 재작성을
요구하거나 교사가 확인해야 할 항목으로 최종 표시한다.

즉 이 스크립트의 출력은 두 소비자를 겨냥한다:
  - 02(재작성 모드): structural_status == "FAIL" 일 때 개입 (block 위반 제거 + 길이 조정)
  - 03(검증):        review_items / warning_items 를 항상 넘겨받아 문맥 판단에 반영

사용법 (두 가지 방식)
---------------------
1) 모듈로 import해서 사용 (마스터 워크플로우가 대화 중 코드 셀에서 직접 호출할 때 권장):

    from cloud_linter import lint
    report = lint(text=draft_text, field_type="subject_setuk", rules_path="rules.json")
    print(report["structural_status"])  # "PASS" | "FAIL"
    print(report["next_step"])          # "03_verification" | "02_drafting"

2) CLI로 직접 실행:

    python cloud_linter.py rules.json subject_setuk draft.txt
    # draft.txt 대신 '-' 를 넘기면 stdin에서 텍스트를 읽는다.

종료 코드 (CLI 실행 시)
------------------------
    0 = structural_status PASS  (block 위반 없음, 길이 정상 -> 03(검증) 단계로 진행)
    1 = structural_status FAIL  (block 위반 또는 길이 초과/미달 -> 02(재작성 모드) 단계로 진행)

주의: review/warning 항목이 있다고 해서 종료 코드가 바뀌지 않는다. 이 항목들은
구조적 실패가 아니라 "평가 단계에서 반드시 검토해야 할 목록"이기 때문이다.

field_type 값
-------------
rules.json의 length_limits.fields 키와 동일해야 한다.
예: subject_setuk, individual_setuk, autonomous_activity, club_activity,
    career_activity, behavior_comment
"""

import json
import re
import sys


# ---------------------------------------------------------------------------
# 1. NEIS 바이트 계산
# ---------------------------------------------------------------------------

def count_neis_bytes(text: str) -> int:
    """
    NEIS 입력 기준 바이트 수 계산.
    한글(가-힣, 자모 포함) = 3byte
    줄바꿈(\n)            = 2byte
    그 외(영문/숫자/기호/공백) = 1byte

    일반적인 UTF-8/EUC-KR 바이트 계산과 다르므로 반드시 이 함수를 통해서만 계산할 것.
    """
    total = 0
    for ch in text:
        if ch == "\n":
            total += 2
        elif "\uac00" <= ch <= "\ud7a3" or "\u1100" <= ch <= "\u11ff" or "\u3130" <= ch <= "\u318f":
            # 완성형 한글 음절 + 한글 자모 영역
            total += 3
        else:
            total += 1
    return total


# ---------------------------------------------------------------------------
# 2. rules.json 로드
# ---------------------------------------------------------------------------

def load_rules(rules_path: str) -> dict:
    with open(rules_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _context_snippet(text: str, idx: int, keyword_len: int, window: int = 15) -> str:
    """위반 지점 주변 문맥을 교사가 바로 확인할 수 있도록 잘라서 반환."""
    start = max(0, idx - window)
    end = min(len(text), idx + keyword_len + window)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


# ---------------------------------------------------------------------------
# 3. 개별 검사 함수
# ---------------------------------------------------------------------------

def check_length(text: str, field_type: str, rules: dict) -> dict:
    limits = rules.get("length_limits", {})
    fields = limits.get("fields", {})
    field_info = fields.get(field_type)

    byte_count = count_neis_bytes(text)

    if field_info is None:
        return {
            "field_type": field_type,
            "byte_count": byte_count,
            "byte_limit": None,
            "status": "unknown_field",
            "message": f"field_type '{field_type}' 이 rules.json에 정의되어 있지 않음. rules.json length_limits.fields 확인 필요.",
        }

    max_bytes = field_info.get("max_bytes")

    if max_bytes is None:
        return {
            "field_type": field_type,
            "byte_count": byte_count,
            "byte_limit": None,
            "status": "unverified",
            "message": "이 필드는 정확한 글자수 기준이 아직 확인되지 않음. "
                       "바이트 수는 참고용으로만 표시하며 자동 pass/fail 판정에서 제외함.",
        }

    if byte_count > max_bytes:
        return {
            "field_type": field_type,
            "byte_count": byte_count,
            "byte_limit": max_bytes,
            "status": "over_limit",
            "message": f"{byte_count}byte / 제한 {max_bytes}byte 초과 ({byte_count - max_bytes}byte 초과분).",
        }

    # 너무 짧은 것도 문제(과제 설계 상 "1400바이트 미만 시 부연 확장" 요구사항 반영)
    min_bytes = int(max_bytes * 0.93)  # 대략 1400/1500 비율을 기본값으로 사용
    if byte_count < min_bytes:
        return {
            "field_type": field_type,
            "byte_count": byte_count,
            "byte_limit": max_bytes,
            "status": "under_min",
            "message": f"{byte_count}byte / 권장 최소 {min_bytes}byte 미달. 02_drafting.md(REWRITE 모드) 기준 부연 확장 필요.",
        }

    return {
        "field_type": field_type,
        "byte_count": byte_count,
        "byte_limit": max_bytes,
        "status": "ok",
        "message": "길이 기준 통과.",
    }


def check_keyword_categories(text: str, rules: dict) -> list:
    violations = []
    categories = rules.get("forbidden_keywords", {}).get("categories", [])

    for cat in categories:
        matched = []
        for kw in cat.get("keywords", []):
            idx = text.find(kw)
            if idx != -1:
                matched.append({
                    "keyword": kw,
                    "position": idx,
                    "context": _context_snippet(text, idx, len(kw)),
                })
        if matched:
            violations.append({
                "source": "forbidden_keywords",
                "category_id": cat["id"],
                "label": cat["label"],
                "basis": cat.get("basis"),
                "severity": cat.get("severity", "review"),
                "matches": matched,
            })

    return violations


def check_combination_flags(text: str, rules: dict, window: int = 20) -> list:
    """가족어 + 직업어가 window 글자 이내에 함께 등장하면 review 플래그."""
    violations = []
    rules_list = rules.get("combination_flags", {}).get("rules", [])

    for rule in rules_list:
        family_terms = rule.get("list_a_family_terms", [])
        occupation_terms = rule.get("list_b_occupation_terms", [])

        family_hits = [(m.start(), t) for t in family_terms for m in re.finditer(re.escape(t), text)]
        occ_hits = [(m.start(), t) for t in occupation_terms for m in re.finditer(re.escape(t), text)]

        matched_pairs = []
        for f_idx, f_term in family_hits:
            for o_idx, o_term in occ_hits:
                if abs(f_idx - o_idx) <= window:
                    matched_pairs.append({
                        "family_term": f_term,
                        "occupation_term": o_term,
                        "context": _context_snippet(text, min(f_idx, o_idx), abs(f_idx - o_idx) + max(len(f_term), len(o_term))),
                    })

        if matched_pairs:
            violations.append({
                "source": "combination_flags",
                "category_id": rule["id"],
                "label": rule["label"],
                "basis": rule.get("basis"),
                "severity": rule.get("severity", "review"),
                "matches": matched_pairs,
            })

    return violations


def check_regex_patterns(text: str, rules: dict) -> list:
    violations = []
    patterns = rules.get("regex_patterns", {}).get("patterns", [])

    for pat in patterns:
        matched = []

        if "pattern" in pat:
            for m in re.finditer(pat["pattern"], text):
                matched.append({
                    "matched_text": m.group(0),
                    "position": m.start(),
                    "context": _context_snippet(text, m.start(), len(m.group(0))),
                })

        if "keywords_fallback" in pat:
            for kw in pat["keywords_fallback"]:
                idx = text.find(kw)
                if idx != -1:
                    matched.append({
                        "matched_text": kw,
                        "position": idx,
                        "context": _context_snippet(text, idx, len(kw)),
                    })

        if matched:
            violations.append({
                "source": "regex_patterns",
                "category_id": pat["id"],
                "label": pat["label"],
                "basis": pat.get("basis"),
                "severity": pat.get("severity", "review"),
                "matches": matched,
            })

    return violations


def check_harness_specific(text: str, rules: dict) -> list:
    violations = []
    hs = rules.get("harness_specific", {})

    for section_key in ("ai_residue_patterns", "vague_praise_only"):
        section = hs.get(section_key)
        if not section:
            continue
        matched = []
        for kw in section.get("keywords", []):
            idx = text.find(kw)
            if idx != -1:
                matched.append({
                    "keyword": kw,
                    "position": idx,
                    "context": _context_snippet(text, idx, len(kw)),
                })
        if matched:
            violations.append({
                "source": "harness_specific",
                "category_id": section_key,
                "label": section.get("label", section_key),
                "basis": None,
                "severity": section.get("severity", "warning"),
                "matches": matched,
            })

    return violations


# ---------------------------------------------------------------------------
# 4. 종합 판정
# ---------------------------------------------------------------------------

def lint(text: str, field_type: str, rules_path: str = "rules.json") -> dict:
    rules = load_rules(rules_path)

    length_result = check_length(text, field_type, rules)

    all_violations = []
    all_violations += check_keyword_categories(text, rules)
    all_violations += check_combination_flags(text, rules)
    all_violations += check_regex_patterns(text, rules)
    all_violations += check_harness_specific(text, rules)

    block_violations = [v for v in all_violations if v["severity"] == "block"]
    review_violations = [v for v in all_violations if v["severity"] == "review"]
    warning_violations = [v for v in all_violations if v["severity"] == "warning"]

    length_fail = length_result["status"] in ("over_limit", "under_min")

    # structural_status: block 위반이나 길이 문제처럼 기계적으로 100% 확실한 것만 FAIL 처리.
    # review/warning은 여기서 판정하지 않고, 03 검증 모듈로 그대로 전달한다.
    structural_status = "FAIL" if (block_violations or length_fail) else "PASS"
    next_step = "02_drafting" if structural_status == "FAIL" else "03_verification"

    return {
        "field_type": field_type,
        "length": length_result,
        "violations": {
            "block": block_violations,
            "review": review_violations,
            "warning": warning_violations,
        },
        "counts": {
            "block": len(block_violations),
            "review": len(review_violations),
            "warning": len(warning_violations),
        },
        "structural_status": structural_status,
        "next_step": next_step,
        "verification_notes": {
            "must_judge_in_context": [
                {
                    "category_id": v["category_id"],
                    "label": v["label"],
                    "basis": v.get("basis"),
                    "matches": v["matches"],
                }
                for v in review_violations
            ],
            "fyi_only": [
                {
                    "category_id": v["category_id"],
                    "label": v["label"],
                    "matches": v["matches"],
                }
                for v in warning_violations
            ],
            "instruction_for_03": (
                "위 must_judge_in_context 항목은 정적 검증기가 기계적으로 찾아낸 후보일 뿐, "
                "실제 위반 여부는 문맥으로만 판단 가능하다(예: 3항-차 부모 직업 암시는 학생 "
                "자신의 진로 희망 서술과 구분해야 함). 검증 모듈(03)은 각 항목에 대해 "
                "'실제 위반' 또는 '정상 서술'을 판단하고, 실제 위반이면 재작성을 요구하며, "
                "판단이 애매하면 '판단 보류'로 남겨 04를 거쳐 교사 확인 항목으로 전달할 것. "
                "품질(구체성, 문체 등) 평가는 이 항목과 무관하며 04의 몫이다."
            ),
        },
        "rules_meta": {
            "source": rules.get("meta", {}).get("source"),
            "length_limits_verified": rules.get("length_limits", {}).get("verified"),
        },
    }


# ---------------------------------------------------------------------------
# 5. CLI 진입점
# ---------------------------------------------------------------------------

def _main():
    if len(sys.argv) != 4:
        print("사용법: python cloud_linter.py <rules.json 경로> <field_type> <텍스트파일 경로 또는 ->", file=sys.stderr)
        sys.exit(3)

    rules_path, field_type, text_arg = sys.argv[1], sys.argv[2], sys.argv[3]

    if text_arg == "-":
        text = sys.stdin.read()
    else:
        with open(text_arg, "r", encoding="utf-8") as f:
            text = f.read()

    report = lint(text=text, field_type=field_type, rules_path=rules_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))

    sys.exit(0 if report["structural_status"] == "PASS" else 1)


if __name__ == "__main__":
    _main()
