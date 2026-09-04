# Setuk-AI Cline 규칙

- 개별 학생 세특 작성 요청에서는 저장소 루트의 `AGENTS.md`에 정의된 세특 파이프라인을 마스터 규칙으로 사용한다.
- 개별 학생 창체(진로활동·자율활동) 작성 요청에서는 `AGENTS.md`의 창체 파이프라인(진입 모드 C)을 마스터 규칙으로 사용한다.
- 세특 보고서 입력 모드에서는 `.agent/01_report_ingestion.md`, `.agent/02_data_structuring.md`, `.agent/03_drafting.md`, `.agent/04_evaluation.md`를 반드시 순서대로 읽는다.
- 창체 모드에서는 `.agent/01c_activity_report_ingestion.md`, `.agent/02_data_structuring.md`, `.agent/03c_activity_drafting.md`, `.agent/04_evaluation.md`를 반드시 순서대로 읽는다.
- "땜빵모드" 시동어가 있는 공통교육 대체 창체 요청(F모드)에서는 `.agent/01f_common_education_drafting.md` 하나만 읽는다. 02·03c·04 단계로 이어지지 않으며, 보고서(`.docx`)를 읽지 않는다.
- 물리학 통합 세특 모드(자기성찰·물리신문·주제탐구 통합 요청)에서는 `.agent/01e_physics_report_ingestion.md`, `.agent/02_data_structuring.md`, `.agent/03e_physics_drafting.md`, `.agent/04_evaluation.md`를 반드시 순서대로 읽는다.
- 사용자가 기존 `학생정보/*.yaml` 경로를 명시한 경우에만 보고서 입력 단계를 생략한다.
- `REPORT_NEEDS_INPUT_V1`이 나오면 YAML을 생성하거나 후속 단계를 실행하지 않는다.
- `NEEDS_INPUT_V1`이 나오면 후속 단계를 실행하지 않는다.
- 최종 파일 덮어쓰기와 매 Linter 실행 전 사용자의 명시적 승인을 받는다.
- 세특(A·B)·창체(C)·물리 통합(E) 모드는 03(또는 03c/03e)의 초안 완성 직후 `AGENTS.md`의 "2-A. Human-in-the-Loop 체크포인트"를 거친다. 교사가 `scripts/review_cli.py`로 승인(run_state status: `REVIEWED`)하기 전에는 04를 실행하지 않는다.
- 개발·설명·테스트 요청을 학생 세특 작성 트리거로 오인하지 않는다.
- Cline의 Plan/Act 모드나 자동 승인 설정이 `AGENTS.md`의 개인정보 경계와 대화형 승인 게이트를 무효화하지 않는다.
- 한글 지침과 YAML은 첫 읽기부터 UTF-8을 명시하며, 기본 인코딩으로 읽은 뒤 재시도하지 않는다.
