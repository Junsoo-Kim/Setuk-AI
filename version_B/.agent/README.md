# 세특·창체 작성 에이전트 파이프라인

이 폴더의 문서는 하나의 AI가 역할을 바꾸어 가며 순서대로 수행하는 독립 지침입니다. 세특과 창체(진로활동·자율활동)는 서로 다른 파이프라인이며, 마스터 규칙(`AGENTS.md`)은 각 파이프라인의 순서를 바꾸거나 생략해서는 안 됩니다.

## 세특 파이프라인

1. `01_report_ingestion.md`: 지정 학생의 DOCX 보고서 1개를 읽고 학생 YAML 생성
2. `02_data_structuring.md`: 학생 YAML을 근거가 추적되는 문장형 명제로 변환
3. `03_drafting.md`: 명제를 동기·탐구·역량 및 성장 구조의 초안(한 문단)으로 변환
4. `04_evaluation.md`: 사실 보존 여부와 내용의 깊이를 평가하고 최종 파일 저장

```text
보고서/<보고서파일>.docx
  → REPORT_INGESTED_V1
  → 학생정보/<학생명>.yaml
  → STRUCTURED_FACTS_V1
  → DRAFT_V1
  → [Human-in-the-Loop 체크포인트: 교사 검토·승인, AGENTS.md 2-A]
  → 세특/<식별자>.md
```

## 창체(진로활동·자율활동) 파이프라인

1. `01c_activity_report_ingestion.md`: 순위가 매겨진 DOCX 보고서 여러 개를 읽고, 활동마다 `priority`·`source_report`를 붙여 학생 YAML 생성
2. `02_data_structuring.md`: 세특과 동일한 지침으로 명제 변환 (공용)
3. `03c_activity_drafting.md`: 활동을 하나의 서사로 합치지 않고, 순위 순서대로 독립된 문장 묶음을 이어 붙인 초안 작성
4. `04_evaluation.md`: 세특과 동일한 지침으로 평가·저장 (공용, 다중 활동 추가 확인 포함)

```text
보고서/<파일A>.docx, <파일B>.docx, <파일C>.docx (순위 지정)
  → REPORT_INGESTED_V1 (priority_map 포함)
  → 학생정보/<학생명>_<항목>.yaml
  → STRUCTURED_FACTS_V1
  → DRAFT_V1 (activities[] 별도 유지)
  → [Human-in-the-Loop 체크포인트: 교사 검토·승인, AGENTS.md 2-A]
  → 창체/<식별자>_<항목>.md
```

- 중간 계약은 현재 작업 컨텍스트 안에서만 전달하고 별도 학생 파일로 저장하지 않습니다.
- 사용자가 기존 YAML 경로를 명시한 직접 입력 모드에서는 `01_report_ingestion.md`만 생략할 수 있습니다.
- `REPORT_NEEDS_INPUT_V1`이 반환되면 YAML을 만들거나 후속 단계를 실행하지 않습니다.
- `NEEDS_INPUT_V1`이 반환되면 이후 단계를 실행하지 않고 교사에게 부족한 근거를 요청합니다.
- 후속 단계는 앞 단계에 없는 사실을 복원하거나 추측할 수 없습니다.
- 최종 파일명은 학생 이름이 아니라 입력 YAML의 파일명을 그대로 사용합니다.
- 정적 Linter 실행과 오류 반복 수정은 루트 `AGENTS.md`와 `.clinerules/`의 마스터 규칙이 연결합니다.

## 물리학 통합 세특 파이프라인

1. `01e_physics_report_ingestion.md`: 자기성찰(선택)·물리신문·주제탐구 보고서를 읽고, 활동마다 `role`·`source_report`를 붙여 학생 YAML 생성
2. `02_data_structuring.md`: 세특과 동일한 지침으로 명제 변환 (공용)
3. `03e_physics_drafting.md`: 활동을 하나의 서사로 합치지 않고, 자기성찰(있으면)→물리신문→주제탐구 순서로 독립된 문장 묶음을 이어 붙인 초안 작성. 자기성찰(태도) 문단은 1문장 원칙, 짧게 고정 배분
4. `04_evaluation.md`: 세특과 동일한 지침으로 평가·저장 (공용, 다중 활동 추가 확인 포함, 자기성찰 문단은 나열식 판정 예외)

```text
보고서/<자기성찰>.docx(선택), <물리신문>.docx, <주제탐구>.docx
  → REPORT_INGESTED_V1 (roles_used 포함)
  → 학생정보/<학생명>_물리세특.yaml
  → STRUCTURED_FACTS_V1
  → DRAFT_V1 (activities[] 별도 유지)
  → [Human-in-the-Loop 체크포인트: 교사 검토·승인, AGENTS.md 2-A]
  → 세특/<식별자>.md
```

- 물리신문·주제탐구가 모두 없거나(0매칭) 모두 품질 미달로 제외되면 YAML을 만들지 않고 작업을 중단, 교사에게 별도 보고한다.
- 최종 출력 경로는 입력 YAML의 `_물리세특` 접미사를 따르지 않고 `세특/<식별자>.md`로 고정한다.

## 창체 분류 전용 모드 (선택)

`01d_activity_classification.md` 하나만 실행하는 독립 모드입니다. 항목(진로활동/자율활동) 지정 없이 학생명만으로 `보고서/`의 `.docx`를 전부 읽어 진로/자율 후보와 순위를 채팅으로만 보고하며, 어떤 YAML·MD 파일도 만들지 않습니다. 위 창체 파이프라인(01c→02→03c→04)과는 별개로, 그 앞 단계를 대신 안내해 주는 보조 기능입니다.

## 공통 불변 규칙

- 입력 YAML과 앞 단계 산출물만 사실의 근거로 사용합니다.
- 보고서 입력 모드에서는 지정 DOCX 하나와 그 문서에서 생성한 YAML만 사용합니다.
- 학생·교사 이름과 학년·반·번호는 파일 식별에만 쓰며 세특 본문에 넣지 않습니다.
- 빈 값은 정보가 없다는 뜻이며 상식이나 전형적인 활동으로 채우지 않습니다.
- 여러 학생 또는 다른 대화에서 얻은 정보를 섞지 않습니다.
- 교사의 직접 관찰처럼 보이는 표현은 `teacher_observation` 또는 구체적인 활동 근거가 있을 때만 사용합니다.
