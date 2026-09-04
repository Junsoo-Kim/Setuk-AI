# Setuk-AI C버전 (자율 멀티에이전트, Claude API 직접 호출)

A버전(`version_A/`, 웹 업로드)과 B버전(`version_B/`, VSCode + 클로드 구독)과는 다른 셋째 트랙이다.
설계 배경과 위치는 저장소 루트의 `기획서.md` 0장·8장·9장을 참고한다.

## B버전과 무엇이 다른가

| | B버전 | C버전 |
| --- | --- | --- |
| 실제 문장 생성 주체 | 교사가 이미 구독 중인 VSCode AI 확장 | 이 코드가 Claude API를 **직접** 호출 |
| 비용 | 구독료 외 추가 없음 | API 사용량만큼 별도 과금 |
| 설치 | 무설치 ZIP | `pip install`, `.env`에 API 키 필요 |
| Human-in-the-Loop 구현 | `run_state/*.json` + `scripts/review_cli.py`(터미널) | LangGraph `interrupt()`/`Command(resume=...)` + 로컬 웹 UI |
| 지원 모드 | 세특 A·B, 창체 C·D, 물리 통합 E, 땜빵 F | **세특(A·B에 해당하는 단일 보고서/YAML) 흐름만** — 나머지는 이후 확장 |

핵심 지침(`version_B/.agent/01_report_ingestion.md` ~ `04_evaluation.md`)은 B버전과 **동일한 파일을
그대로** 시스템 프롬프트로 재사용한다(`prompts.py`). 두 버전이 서로 다른 판단 기준을 갖지 않게 하기
위함이다. Linter(`version_B/linter.py`)와 DOCX 추출(`version_B/scripts/extract_docx.py`)도 마찬가지로
재사용한다(`bridge.py`가 `version_B/`를 기준 경로로 잡는다).

## 설치

```powershell
cd version_C
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# .env를 열어 ANTHROPIC_API_KEY를 채운다
```

포터블 Python(`version_B/python_portable/`)은 B버전 전용이며 여기서는 쓰지 않는다. 시스템에 설치된
Python 3.10 이상이 필요하다.

## 실행

저장소 루트에서:

```powershell
python version_C\run_server.py
```

브라우저에서 `http://127.0.0.1:5000`을 연다.

1. 학생 식별자와 입력 방식(기존 YAML 또는 DOCX 보고서)을 지정해 작업을 시작한다.
2. AI가 보고서 입력 → 구조화 → 초안 작성을 마치면 자동으로 멈추고 웹 화면에 초안을 보여준다.
3. 그대로 승인하거나, 텍스트 상자에서 직접 고친 뒤 승인한다. 마음에 안 들면 사유를 적어 반려하면
   그 사유를 반영해 다시 초안을 작성한다.
4. 승인하면 평가·최종 저장·Linter 검증까지 자동으로 진행되어 `version_B/세특/<식별자>.md`에 저장된다.
   Linter 오류가 있으면 최대 5회까지 스스로 고쳐 재검사한다(B버전 AGENTS.md 5번과 동일한 상한).

입력 경로(YAML/DOCX)는 각각 `version_B/학생정보/`, `version_B/보고서/` 밖을 가리키면 거부된다.

## 상태 저장

진행 상태는 `version_C/run_state_c.sqlite3`(LangGraph SQLite 체크포인터)에 저장되므로 서버를
껐다 켜도 진행 중이던 작업을 이어갈 수 있다. 이 파일과 `.env`는 학생 식별 가능한 초안을 담을 수
있으므로 `.gitignore`로 추적하지 않는다.

## 알려진 제약 (v1)

- 세특(단일 보고서/YAML) 흐름만 구현되어 있다. 창체·물리 통합·땜빵모드는 아직 없다.
- 01단계에서 `REPORT_NEEDS_INPUT_V1`(정보 부족)이 나오면 B버전처럼 채팅으로 되묻는 대신 그 작업을
  오류로 종료한다. 부족한 정보를 채운 뒤 새로 시작해야 한다.
- 리뷰 화면은 단일 로컬 사용자를 전제로 한다(여러 교사가 동시에 같은 서버를 쓰는 인증·권한 분리는
  없음).
- 이 결과물도 B버전과 마찬가지로 최신 학교생활기록부 기재요령 준수를 자동으로 보증하지 않는다.
  최종 확인 책임은 교사에게 있다.

## 테스트

C버전 테스트는 일부러 `version_B/tests/`가 아니라 `version_C/tests/`에 따로 둔다.
`version_B/tests/`는 포터블 Python(외부 의존성 없음)으로 돌아가는 B버전 테스트 스위트라서,
`langgraph`·`anthropic`이 설치되어 있지 않은 그 환경에서 실행하면 즉시 임포트 오류가 난다.
C버전 테스트는 이 폴더의 `.venv`(시스템 Python)로만 실행한다.

저장소 루트에서 (venv 활성화 후):

```powershell
python -m unittest discover -s version_C\tests -v
```

이 테스트는 실제 Claude API를 호출하지 않고 `call_agent`를 가짜 응답으로 대체해 그래프 배선과
interrupt/resume, Linter 재시도 루프, 경로 안전성 검증을 확인한다.
