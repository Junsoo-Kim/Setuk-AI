# Setuk-AI

고등학교 교사의 세부능력 및 특기사항(세특) 작성을 돕는 로컬 VSCode 기반 AI 하네스입니다.

현재 개발 상태는 **4단계 완료: Codex/Cline 마스터 규칙과 Linter 승인·자동 수정 흐름 연동**입니다.

## 현재 사용할 수 있는 것

- `학생정보/template.yaml`: 새 학생 파일을 만들 때 사용하는 원본 양식
- `학생정보/example.yaml`: 입력 방법을 보여 주는 가상 학생 예시
- `학생정보/README.md`: 항목별 작성 원칙과 주의사항
- `세특/`: 이후 AI가 완성한 세특 문서를 저장할 위치
- `linter.py`: 세특 결과물의 분량·금칙어·허용 문자를 검사하는 도구
- `rules.json`: 학교와 학년도에 맞게 수정할 수 있는 검사 규칙
- `.agent/01_data_structuring.md`: YAML을 근거 추적이 가능한 명제로 구조화하는 지침
- `.agent/02_drafting.md`: 학습 동기·탐구 활동·역량 및 성장 구조의 초안 작성 지침
- `.agent/03_evaluation.md`: 사실 보존 여부와 내용의 깊이를 평가하고 최종 파일을 저장하는 지침
- `AGENTS.md`: Codex와 Cline이 공통으로 사용하는 전체 실행·승인·수정 규칙
- `.clinerules/00-setuk-master.md`: Cline 전용 보완 규칙
- `.clinerules/workflows/setuk.md`: Cline에서 선택적으로 호출할 수 있는 세특 작성 워크플로

세 AI 지침은 `STRUCTURED_FACTS_V1 → DRAFT_V1 → EVALUATED_RESULT_V1` 계약으로 연결됩니다. 입력 근거가 부족하면 `NEEDS_INPUT_V1`으로 중단하여 AI가 빈 내용을 추측해 채우지 않도록 설계했습니다.

## AI 파이프라인 실행 방법

VSCode에서 프로젝트 폴더를 열고 Codex 또는 Cline의 새 채팅에 다음과 같이 입력합니다.

```text
김준수 세특 작업 시작해
```

이 경우 `학생정보/김준수.yaml`이 정확히 존재해야 합니다. 파일명이 학생 이름이 아니라 내부 식별번호라면 다음처럼 정확한 경로를 지정합니다.

```text
학생정보/2026-2-03-12.yaml로 세특 작성해
```

AI는 3개 지침을 순서대로 실행하고 최종 파일을 저장한 뒤 Linter 명령을 보여 줍니다. 사용자가 명시적으로 승인해야 검사가 실행되며, 오류 수정 후 재검사할 때도 다시 승인을 요청합니다.

Cline에서는 자연어 요청 외에 `/setuk.md` 워크플로를 선택적으로 사용할 수 있습니다. Codex의 현재 공식 프로젝트 지침 파일은 `.codexrules`가 아니라 `AGENTS.md`이므로 이 저장소도 해당 형식을 사용합니다. Cline 역시 `AGENTS.md`를 읽으며, Cline 전용 규칙은 `.clinerules/`에 보관합니다.

## 학생정보 작성 방법

1. `학생정보/template.yaml`을 같은 폴더에 복사합니다.
2. 복사한 파일 이름을 학생을 구분할 수 있는 이름으로 바꿉니다.
3. 파일의 안내 주석을 참고하여 확인된 사실만 입력합니다.
4. 해당 사항이 없는 목록은 빈 배열(`[]`)로 유지합니다.

실제 학생의 개인정보가 Git 저장소에 올라가지 않도록 학생별 YAML 파일은 기본적으로 무시됩니다. 배포에 포함할 `template.yaml`과 가상 데이터인 `example.yaml`만 추적합니다.

## Linter 실행 방법

프로젝트 루트에서 다음 명령을 실행합니다.

```powershell
.\python-3.13.15-embed-amd64\python.exe linter.py "세특\학생파일.md"
```

AI가 결과를 구조적으로 읽어야 할 때는 JSON 출력을 사용할 수 있습니다.

```powershell
.\python-3.13.15-embed-amd64\python.exe linter.py "세특\학생파일.md" --json
```

특정 과목이나 학교 기준에 맞춰 최대 분량만 일시적으로 바꾸려면 다음과 같이 실행합니다.

```powershell
.\python-3.13.15-embed-amd64\python.exe linter.py "세특\학생파일.md" --max-bytes 1200
```

종료 코드는 `0`이면 통과, `1`이면 내용 규칙 위반, `2`이면 파일 또는 설정 오류를 의미합니다. 기본 분량은 `rules.json`의 1,500바이트이며 ASCII 1바이트, 비ASCII 3바이트, 줄바꿈 2바이트로 계산합니다. 실제 허용 분량과 기재 제한은 과목·학년·학년도에 따라 달라질 수 있으므로 해당 연도의 학교생활기록부 기재요령에 맞춰 `rules.json`을 확인해야 합니다.

## 자동 테스트

외부 패키지 없이 포터블 Python으로 실행됩니다.

```powershell
.\python-3.13.15-embed-amd64\python.exe -m unittest discover -s tests -v
```

## 예정된 개발 단계

1. 프로젝트 기반 및 학생정보 입력 규격 구축 — 완료
2. NEIS 기준 정적 분석 Linter 및 자동 테스트 개발 — 완료
3. 전처리·초안 작성·평가 AI 지침 개발 — 완료
4. Codex/Cline 마스터 규칙과 자동 수정 흐름 연동 — 완료
5. 포터블 Python 포함 배포 패키지 및 사용자 문서 완성

자세한 목표와 전체 흐름은 `기획서.md`에서 확인할 수 있습니다.
