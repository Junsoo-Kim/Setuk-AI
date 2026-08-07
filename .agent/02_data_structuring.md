# 02. 학생정보 구조화

## 역할

학생정보 YAML의 짧은 키워드를 사실 근거가 추적되는 문장형 명제로 변환한다. 이 단계의 목적은 글을 아름답게 쓰는 것이 아니라 다음 단계가 추측 없이 사용할 수 있는 사실 장부를 만드는 것이다.

## 입력

- `source_path`: `학생정보/<식별자>.yaml`
- YAML 내용: `schema_version: 1` 형식

입력 파일은 한 번만 선택하며 같은 작업에서 다른 학생 YAML을 함께 읽지 않는다. `template.yaml`과 `example.yaml`은 실제 작업 입력으로 사용하지 않는다.

## 절대 규칙

1. 입력에 없는 행동, 도구, 수치, 결과, 감정, 동기, 역할, 역량을 추가하지 않는다.
2. 빈 문자열, 빈 배열, `null`은 정보가 없는 것으로 처리한다.
3. `competencies`는 교사가 제시한 평가 후보이다. `process`, `evidence`, `result`, `teacher_observation`에서 뒷받침될 때만 확정 역량으로 분류한다.
4. 원인과 결과가 입력에서 직접 연결되지 않으면 인과관계로 만들지 않고 시간적 순서 또는 병렬 사실로 둔다.
5. 여러 활동의 동기·과정·결과를 서로 교차 결합하지 않는다.
6. 학생 이름, 교사 이름, 학년, 반, 번호는 본문용 명제로 만들지 않는다.
7. 문장을 세특 문체로 과장하지 않는다. 평가와 수사는 다음 단계의 책임이다.

## 처리 절차

1. 최상위 필수 키 `schema_version`, `student`, `subject`, `activities`, `requirements`를 확인한다.
2. 과목명과 학기를 확인하되 학생 식별정보와 분리한다.
3. 활동마다 다음 사실을 독립적으로 추출한다.
   - 시작 계기: `motivation`
   - 실제 행동: `process`
   - 확인 가능한 근거: `evidence`
   - 결론 또는 산출물: `result`
   - 변화: `growth`
   - 교사의 직접 관찰: `teacher_observation`
4. 각 사실을 주어가 학생으로 해석되는 간결한 문장형 명제로 바꾼다.
5. 명제마다 원본 YAML 경로를 붙인다. 예: `activities[0].process[1]`.
6. 확정 역량은 이를 지지하는 명제 ID와 함께 기록한다.
7. `requirements.emphasis`와 `requirements.exclude`를 그대로 보존한다.

## 중단 조건

다음 중 하나라도 해당하면 `STRUCTURED_FACTS_V1` 대신 `NEEDS_INPUT_V1`만 반환하고 종료한다.

- 과목명이 비어 있음
- `activities`가 없거나 실제 내용이 있는 활동이 하나도 없음
- 모든 활동에서 `process`, `evidence`, `result`, `teacher_observation`이 전부 비어 있음
- 서로 양립할 수 없는 수치나 결과가 있어 어느 쪽이 맞는지 판단할 수 없음

보완 질문은 한 번에 답할 수 있도록 구체적으로 작성한다. 질문에서 답을 유도하거나 사실을 제안하지 않는다.

## 출력 계약: `NEEDS_INPUT_V1`

```yaml
contract: NEEDS_INPUT_V1
source_path: "학생정보/<식별자>.yaml"
questions:
  - field: "activities[0].process"
    question: "학생이 실제로 수행한 절차를 확인해 주세요."
```

## 출력 계약: `STRUCTURED_FACTS_V1`

아래 필드만 사용한다. `proposition`은 반드시 완결된 문장형 명제로 작성한다.

```yaml
contract: STRUCTURED_FACTS_V1
source_path: "학생정보/<식별자>.yaml"
output_path: "세특/<식별자>.md"
subject:
  name: ""
  semester: ""
constraints:
  target_bytes: null
  emphasis: []
  exclude: []
activities:
  - activity_id: A1
    topic: ""
    propositions:
      - id: A1-P1
        role: motivation
        proposition: ""
        evidence_paths: ["activities[0].motivation"]
    supported_competencies:
      - competency: ""
        support_ids: ["A1-P2", "A1-P3"]
overall_observation:
  proposition: ""
  evidence_paths: ["overall_observation"]
unused_fields: []
```

`role`은 `motivation`, `action`, `evidence`, `result`, `growth`, `observation` 중 하나만 사용한다. 내용 없는 항목은 빈 명제를 만들지 말고 생략한다.

## 자체 점검

- 모든 명제가 하나 이상의 실제 YAML 경로를 가리키는가?
- 명제의 동사와 수치가 원본보다 강해지지 않았는가?
- 역량마다 구체적인 행동 또는 근거 명제 ID가 있는가?
- 학생 식별정보가 명제에 들어가지 않았는가?
- 제외 조건을 변형하지 않고 보존했는가?
