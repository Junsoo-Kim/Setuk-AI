"""LLM 응답 계약의 Pydantic 스키마와 단계 간 정합성 검증.

기존 `llm.parse_contract`는 yaml 블록이 있고 `contract` 필드가 있는지만 확인했다.
모델이 필드를 빠뜨리거나 엉뚱한 단계의 계약을 돌려줘도 파이프라인 한참 뒤에서야
KeyError로 터졌다. 이 모듈은 단계별 스키마를 discriminated union으로 분리해
파싱 시점에 실패시키고, 실패 사유를 repair 프롬프트에 그대로 쓸 수 있는 문자열로 만든다.

여기서 하는 검증은 파일 시스템을 건드리지 않는 구문·구조 검증이다. 실제 경로가
version_B/학생정보/ 안에 있는지는 pipeline._resolve_within이 한 번 더 확인한다
(모델 출력은 두 겹으로 막는다).
"""

from __future__ import annotations

import posixpath
import re
from typing import Annotated, Any, Callable, Literal, Union

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

STUDENT_DIR = "학생정보"
OUTPUT_DIR = "세특"
REPORT_DIR = "보고서"

PROPOSITION_ID_RE = re.compile(r"^[A-Za-z]\w*-P\d+$")


class ContractValidationError(Exception):
    """계약 스키마 또는 단계 간 정합성 위반. 메시지는 repair 프롬프트에 그대로 쓴다."""


def _normalize_rel_path(value: str, allowed_dir: str, allowed_suffix: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("경로가 비어 있습니다.")
    candidate = value.strip().replace("\\", "/")
    if candidate.startswith("/") or re.match(r"^[A-Za-z]:", candidate):
        raise ValueError(f"절대 경로는 허용하지 않습니다: {value}")
    if ".." in candidate.split("/"):
        raise ValueError(f"상위 디렉터리 참조는 허용하지 않습니다: {value}")
    normalized = posixpath.normpath(candidate)
    head = normalized.split("/", 1)[0]
    if head != allowed_dir:
        raise ValueError(f"{allowed_dir}/ 아래 경로여야 합니다: {value}")
    if not normalized.casefold().endswith(allowed_suffix.casefold()):
        raise ValueError(f"확장자가 {allowed_suffix}가 아닙니다: {value}")
    return normalized


def _student_path(value: str) -> str:
    return _normalize_rel_path(value, STUDENT_DIR, ".yaml")


def _output_path(value: str) -> str:
    return _normalize_rel_path(value, OUTPUT_DIR, ".md")


def _report_path(value: str) -> str:
    return _normalize_rel_path(value, REPORT_DIR, ".docx")


SafeStudentPath = Annotated[str, AfterValidator(_student_path)]
SafeOutputPath = Annotated[str, AfterValidator(_output_path)]
SafeReportPath = Annotated[str, AfterValidator(_report_path)]


class _Contract(BaseModel):
    """모든 계약의 공통 설정.

    `extra="forbid"` — 지침이 정의하지 않은 필드를 모델이 끼워 넣으면 거부한다.
    느슨하게 두면(`allow`) 모델이 `confidence: 0.9`처럼 그럴듯하지만 아무도 검증하지
    않는 필드를 지어내도 그냥 통과하고, 그 값이 하류에서 근거인 척 쓰일 수 있다.
    계약의 요점은 "무엇이 올지 미리 정하는 것"이므로 잠그는 쪽이 맞다.

    **대가**: `.agent/*.md` 지침에 필드가 추가되면 여기도 함께 고쳐야 한다. 그래서
    아래 모델은 지침의 계약 블록과 필드 단위로 일치시켜 두었고, 어긋나면
    `test_contracts.py`가 잡는다.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Question(_Contract):
    field: str = ""
    question: str = ""


class ReportNeedsInputV1(_Contract):
    contract: Literal["REPORT_NEEDS_INPUT_V1"]
    report_path: str = ""
    active_report_directory: str | None = None
    available_docx_filenames: list[str] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)


class ReportIngestedV1(_Contract):
    """01단계. yaml_body는 이 코드가 파일 저장을 대신하기 위해 추가로 요구하는 필드다."""

    contract: Literal["REPORT_INGESTED_V1"]
    yaml_path: SafeStudentPath
    yaml_body: str = Field(min_length=1)
    report_path: str | None = None
    student_name: str | None = None
    saved: bool | None = None
    activity_count: int | None = None
    uncertain_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    excluded_uncertain_facts: list[str] = Field(default_factory=list)


class Proposition(_Contract):
    id: str
    role: (
        Literal["motivation", "action", "evidence", "result", "growth", "observation"] | None
    ) = None
    proposition: str = ""
    evidence_paths: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_id(self) -> "Proposition":
        if not PROPOSITION_ID_RE.match(self.id):
            raise ValueError(f"명제 ID 형식이 A1-P1과 다릅니다: {self.id!r}")
        return self


class SupportedCompetency(_Contract):
    competency: str = ""
    support_ids: list[str] = Field(default_factory=list)


class Activity(_Contract):
    activity_id: str = ""
    topic: str = ""
    propositions: list[Proposition] = Field(default_factory=list)
    supported_competencies: list[SupportedCompetency] = Field(default_factory=list)
    # 02 지침 처리절차 8: 다중 보고서 모드(창체 C, 물리 통합 E)가 붙인 라우팅 메타데이터를
    # 그대로 옮긴다. C버전은 단일 보고서 흐름만 쓰지만 지침 파일을 B와 공유하므로
    # 모델이 넣을 수 있다.
    priority: int | str | None = None
    source_report: str | None = None
    role: str | None = None


class Observation(_Contract):
    proposition: str = ""
    evidence_paths: list[str] = Field(default_factory=list)


class Subject(_Contract):
    name: str = ""
    semester: str = ""


class Constraints(_Contract):
    target_bytes: int | None = None
    emphasis: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class NeedsInputV1(_Contract):
    contract: Literal["NEEDS_INPUT_V1"]
    source_path: str = ""
    questions: list[Question] = Field(default_factory=list)


class StructuredFactsV1(_Contract):
    contract: Literal["STRUCTURED_FACTS_V1"]
    source_path: SafeStudentPath
    output_path: SafeOutputPath
    subject: Subject | None = None
    constraints: Constraints | None = None
    activities: list[Activity] = Field(default_factory=list)
    overall_observation: Observation | None = None
    unused_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_internal_references(self) -> "StructuredFactsV1":
        known = self.proposition_ids()
        duplicates = _duplicates(
            [item.id for activity in self.activities for item in activity.propositions]
        )
        if duplicates:
            raise ValueError(f"명제 ID가 중복되었습니다: {sorted(duplicates)}")
        for activity in self.activities:
            for competency in activity.supported_competencies:
                dangling = [ref for ref in competency.support_ids if ref not in known]
                if dangling:
                    raise ValueError(
                        f"supported_competencies의 support_ids가 실제 명제에 없습니다: {dangling}"
                    )
        return self

    def proposition_ids(self) -> set[str]:
        return {item.id for activity in self.activities for item in activity.propositions}


class TeacherEvaluation(_Contract):
    competency: str = ""
    support_ids: list[str] = Field(default_factory=list)
    expression: str = ""


class SentenceEvidence(_Contract):
    text: str = ""
    source_ids: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class DraftV1(_Contract):
    contract: Literal["DRAFT_V1"]
    source_path: SafeStudentPath
    output_path: SafeOutputPath
    text: str = Field(min_length=1)
    used_proposition_ids: list[str] = Field(default_factory=list)
    omitted_proposition_ids: list[str] = Field(default_factory=list)
    teacher_evaluation: TeacherEvaluation | None = None
    target_byte_range: list[int] | None = None
    estimated_byte_count: int | None = None
    length_exception_reason: str | None = None


class DraftV2(_Contract):
    contract: Literal["DRAFT_V2"]
    source_path: SafeStudentPath
    output_path: SafeOutputPath
    text: str = Field(min_length=1)
    sentences: list[SentenceEvidence] = Field(min_length=1)
    omitted_proposition_ids: list[str] = Field(default_factory=list)
    teacher_evaluation: TeacherEvaluation | None = None
    target_byte_range: list[int] | None = None
    estimated_byte_count: int | None = None
    length_exception_reason: str | None = None

    @property
    def used_proposition_ids(self) -> list[str]:
        seen: list[str] = []
        for sentence in self.sentences:
            for proposition_id in sentence.source_ids:
                if proposition_id not in seen:
                    seen.append(proposition_id)
        return seen


def _migrate_draft_v1_to_v2(v1: DraftV1) -> DraftV2:
    return DraftV2(
        contract="DRAFT_V2",
        source_path=v1.source_path,
        output_path=v1.output_path,
        text=v1.text,
        sentences=[SentenceEvidence(text=v1.text, source_ids=list(v1.used_proposition_ids))],
        omitted_proposition_ids=v1.omitted_proposition_ids,
        teacher_evaluation=v1.teacher_evaluation,
        target_byte_range=v1.target_byte_range,
        estimated_byte_count=v1.estimated_byte_count,
        length_exception_reason=v1.length_exception_reason,
    )


STAGE_MIGRATIONS: dict[str, Callable[[BaseModel], BaseModel]] = {
    "DRAFT_V1": _migrate_draft_v1_to_v2,
}


class Review(_Contract):
    """04 지침의 자체 점검 결과. 각 항목은 satisfied / 그 밖의 진단 문자열이다."""

    factual_fidelity: str = ""
    specificity: str = ""
    coherence: str = ""
    competency_evidence: str = ""
    growth: str = ""
    information_efficiency: str = ""
    teacher_evaluation: str = ""


class EvaluatedResultV1(_Contract):
    """04단계와 lint 수정 단계. final_text도 저장 대행을 위해 추가로 요구한다."""

    contract: Literal["EVALUATED_RESULT_V1"]
    final_text: str = Field(min_length=1)
    source_path: SafeStudentPath | None = None
    output_path: SafeOutputPath | None = None
    saved: bool | None = None
    used_proposition_ids: list[str] = Field(default_factory=list)
    byte_count: int | None = None
    target_byte_range: list[int] | None = None
    length_exception_reason: str | None = None
    review: Review | None = None


AnyContract = Annotated[
    Union[
        ReportIngestedV1,
        ReportNeedsInputV1,
        StructuredFactsV1,
        NeedsInputV1,
        DraftV1,
        DraftV2,
        EvaluatedResultV1,
    ],
    Field(discriminator="contract"),
]

_ADAPTER: TypeAdapter[Any] = TypeAdapter(AnyContract)

STAGE_EXPECTATIONS: dict[str, tuple[type[BaseModel], ...]] = {
    "ingest": (ReportIngestedV1, ReportNeedsInputV1),
    "structure": (StructuredFactsV1, NeedsInputV1),
    "draft": (DraftV1, DraftV2),
    "evaluate": (EvaluatedResultV1,),
    "fix": (EvaluatedResultV1,),
}


def _duplicates(values: list[str]) -> set[str]:
    seen: set[str] = set()
    found: set[str] = set()
    for value in values:
        if value in seen:
            found.add(value)
        seen.add(value)
    return found


def _contract_name(model: type[BaseModel]) -> str:
    return model.model_fields["contract"].annotation.__args__[0]


def format_validation_error(exc: ValidationError) -> str:
    """Pydantic 오류를 모델이 고칠 수 있는 지시 목록으로 바꾼다."""
    lines = []
    for error in exc.errors():
        location = ".".join(
            str(part) for part in error["loc"] if not str(part).endswith("-after")
        )
        lines.append(f"- {location or '(최상위)'}: {error['msg']}")
    return "\n".join(lines)


def parse_stage_contract(data: Any, stage: str) -> BaseModel:
    """계약 dict를 해당 단계에서 허용된 스키마로 검증한다.

    단계에서 기대하지 않는 계약 타입(예: 03단계가 STRUCTURED_FACTS_V1을 반환)도 오류다.
    """
    allowed = STAGE_EXPECTATIONS[stage]
    allowed_names = [_contract_name(model) for model in allowed]
    contract_name = data.get("contract") if isinstance(data, dict) else None
    if contract_name not in allowed_names:
        raise ContractValidationError(
            f"- contract: 이 단계는 {' 또는 '.join(allowed_names)}만 허용합니다. "
            f"받은 값: {contract_name!r}"
        )
    try:
        parsed = _ADAPTER.validate_python(data)
    except ValidationError as exc:
        raise ContractValidationError(format_validation_error(exc)) from exc
    migrate = STAGE_MIGRATIONS.get(_contract_name(type(parsed)))
    return migrate(parsed) if migrate is not None else parsed


def validate_draft_against_facts(draft: DraftV2, facts: StructuredFactsV1) -> None:
    """초안이 사실 장부 밖의 명제를 인용하거나 다른 학생 경로로 새지 않았는지 확인한다."""
    known = facts.proposition_ids()
    dangling = [ref for ref in draft.used_proposition_ids if ref not in known]
    if dangling:
        raise ContractValidationError(
            f"- used_proposition_ids: STRUCTURED_FACTS_V1에 없는 명제 ID를 인용했습니다: {dangling}. "
            f"사용 가능한 ID: {sorted(known) or '(없음)'}"
        )
    _assert_same_paths(draft, facts, "DRAFT_V2")


def validate_result_against_facts(result: EvaluatedResultV1, facts: StructuredFactsV1) -> None:
    known = facts.proposition_ids()
    dangling = [ref for ref in result.used_proposition_ids if ref not in known]
    if dangling:
        raise ContractValidationError(
            f"- used_proposition_ids: STRUCTURED_FACTS_V1에 없는 명제 ID를 인용했습니다: {dangling}."
        )
    _assert_same_paths(result, facts, "EVALUATED_RESULT_V1")


def _assert_same_paths(current: BaseModel, facts: StructuredFactsV1, label: str) -> None:
    for field in ("source_path", "output_path"):
        value = getattr(current, field, None)
        expected = getattr(facts, field)
        if value is not None and value != expected:
            raise ContractValidationError(
                f"- {field}: {label}의 {field}가 직전 단계와 다릅니다. "
                f"기대값 {expected!r}, 받은 값 {value!r}. 같은 학생의 경로를 유지하세요."
            )


def verify_byte_count(claimed: int | None, actual: int) -> str | None:
    """모델이 주장한 byte_count를 코드가 다시 센 값과 대조한다.

    분량은 Linter가 어차피 다시 판정하므로 여기서 파이프라인을 끊지는 않고,
    불일치 사실만 감사 로그용 경고 문자열로 돌려준다.
    """
    if claimed is None or claimed == actual:
        return None
    return (
        f"모델이 보고한 byte_count({claimed})가 코드가 계산한 값({actual})과 다릅니다. "
        "코드 계산값을 신뢰합니다."
    )
