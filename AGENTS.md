# Setuk-AI 개발 지침 (코딩 에이전트용)

이 문서는 이 저장소의 **코드를 고치는** 코딩 에이전트를 위한 개발 규칙이다.
[`version_B/AGENTS.md`](version_B/AGENTS.md)는 교사가 세특 파이프라인을 실행할 때 쓰는
**제품 지침**이며 완전히 별개다 — 세특·창체 작성 요청이 아닌 일반 개발 작업에는
`version_B/AGENTS.md`를 적용하지 않는다.

## 저장소 구조

- `version_A/`: 웹 업로드용, 동결됨.
- `version_B/`: 로컬 VSCode + Claude Code 구독 기반. 자기완결적이며 `python_portable`로
  테스트한다.
- `version_C/`: Claude API 직접 호출. `version_B/.agent`·`version_B/rules.json`·
  `version_B/linter.py`를 의도적으로 재사용한다(`version_C/bridge.py`,
  `version_C/prompts.py`). `version_C/.venv`로 테스트한다.

## 테스트 우선 개발

1. **구현 전에 재현 테스트 또는 실패 조건을 먼저 정의한다.** 버그 수정이든 새 기능이든,
   먼저 그 기능이 지켜야 할 요구사항을 실패하는 테스트로 표현하고, 그 테스트가
   기존 코드에서 **왜** 실패하는지 설명할 수 있어야 구현으로 넘어간다. "테스트와
   구현을 동시에 맡기지 않는다" — 같은 요구사항 오해가 양쪽에 함께 들어갈 수 있다.
2. **기존 테스트를 삭제하거나 assertion을 약화해서 통과시키지 않는다.** 테스트를
   고쳐야 할 이유가 생기면(요구사항 자체가 바뀐 경우 등) 왜 바뀌었는지 커밋 메시지나
   완료 보고에 명시한다. 조용히 완화하지 않는다.
3. **버그 수정 커밋에는 그 버그를 재현하는 테스트를 포함한다.** 재현 테스트 없이
   "고쳤다"고 보고하지 않는다.

## 테스트 데이터와 외부 호출

- 테스트 데이터에는 실제 학생 정보(실명·학번·실제 세특 원문)를 사용하지 않는다.
  가상의 이름·활동을 쓴다.
- 자동 테스트(단위·통합 테스트 스위트)에서는 외부 LLM API(Anthropic API 등)를 호출하지
  않는다. `version_C`는 `llm.call_agent`를 고정 응답으로 대체하는 FakeAgent 패턴을
  쓴다 (`version_C/tests/test_pipeline.py` 참고).
- 실제 모델 호출이 필요한 평가(품질 확인)는 다음 상황에서만, 별도로 수행한다: 모델
  버전 변경, 시스템 프롬프트 변경, 새 교육 기록 유형 추가, 정식 배포 직전. 이때도
  고정된 가상 데이터셋만 사용한다.

## 실행 환경

- B버전은 반드시 `version_B/python_portable/python.exe`로 테스트한다. 시스템 Python이나
  `python`/`python3`/`py`로 대체하지 않는다.
- C버전은 반드시 `version_C/.venv`로 테스트한다.

## 검증 게이트

단일 진입점 [`scripts/test.ps1`](scripts/test.ps1)을 사용한다. 상황에 따라 임의의
명령을 고르지 않는다.

```powershell
.\scripts\test.ps1 -Scope Changed   # 커밋 직전: 변경된 버전(B·C)의 전체 로컬 테스트
.\scripts\test.ps1 -Scope Full      # Push/PR 전: B·C 전체 테스트
.\scripts\test.ps1 -Scope Release   # 배포 직전: 전체 테스트 + 실제 ZIP 생성 + 압축본 연기 검사
```

- 변경된 영역의 테스트를 통과했다고 바로 완료 보고하지 않는다 — 최소한 `-Scope Changed`
  (해당 버전 전체 로컬 테스트)까지 실행한다.
- `version_B/` 또는 `version_C/`의 배포·패키징 관련 코드(`build_release.ps1`,
  `create_release_zip.py`, 화이트리스트 등)를 변경했다면 `-Scope Release`까지 완료한다.
- 완료 보고에는 실행한 명령과 테스트 결과(통과/실패 수, 실패했다면 원인)를 그대로
  포함한다. "테스트를 돌렸다"고만 말하지 않는다.

## 테스트를 추가할 때 참고할 위치

- 회귀 테스트: 버그를 고칠 때 그 버그를 재현하는 케이스를 해당 모듈의 기존 테스트
  파일에 추가한다.
- 프롬프트·계약 정합성: `version_C/tests/test_prompt_contract_sync.py`(`.agent/*.md`
  선언 필드 ↔ `contracts.py` Pydantic 필드),
  `version_C/tests/test_prompt_version.py`(지침·`rules.json` 변경이 `prompt_version()`에
  반영되는지, C가 B 원본을 그대로 참조하는지).
- 전체 흐름: `version_C/tests/test_pipeline.py`(FakeAgent로 실제 API 없이 파이프라인
  시나리오 검증), `version_C/tests/test_server.py`(재시작 후 체크포인트 복구 등).
- 개인정보·악성 입력: `version_C/tests/test_privacy.py`, `version_C/tests/test_mcp.py`.
- 배포 산출물: `version_B/tests/test_release_layout.py`(빌드 스크립트 문구 검증),
  `version_B/tests/test_release_artifact.py`(실제 ZIP을 만들어 화이트리스트·포터블
  실행을 검증).
- DB 스키마: `version_C/tests/test_db_migrations.py`(빈 DB에서 `alembic upgrade head`,
  ORM 모델과 실제 스키마 일치 여부).
