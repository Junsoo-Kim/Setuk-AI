# Setuk-AI Cline 규칙

- 개별 학생 세특 작성 요청에서는 저장소 루트의 `AGENTS.md`에 정의된 세특 파이프라인을 마스터 규칙으로 사용한다.
- `.agent/01_data_structuring.md`, `.agent/02_drafting.md`, `.agent/03_evaluation.md`를 반드시 순서대로 읽는다.
- `NEEDS_INPUT_V1`이 나오면 후속 단계를 실행하지 않는다.
- 최종 파일 덮어쓰기와 매 Linter 실행 전 사용자의 명시적 승인을 받는다.
- 개발·설명·테스트 요청을 학생 세특 작성 트리거로 오인하지 않는다.
- Cline의 Plan/Act 모드나 자동 승인 설정이 `AGENTS.md`의 개인정보 경계와 대화형 승인 게이트를 무효화하지 않는다.
