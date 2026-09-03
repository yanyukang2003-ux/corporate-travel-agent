"""评测什么：工作流/意图评测数据集的 schema、加载与确定性执行。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from corporate_travel_agent.agent.orchestrator import TripWorkflowOrchestrator
from corporate_travel_agent.agent.ports import WorkflowTraceObserverPort
from corporate_travel_agent.domain.constraints import HardConstraint, SoftPreference
from corporate_travel_agent.domain.enums import PolicyOutcome, TaskState, TransportMode
from corporate_travel_agent.domain.models import (
    EmployeeProfileSnapshot,
    HotelOffer,
    LevelTravelRule,
    PolicySnapshot,
    TransportOffer,
    TravelOptionVersion,
    TripRequestVersion,
    TripTask,
)
from corporate_travel_agent.providers.mock import MockProvider
from corporate_travel_agent.services.repositories import (
    InMemoryEmployeeDirectory,
    InMemoryPolicyRepository,
    InMemoryTaskRepository,
)

EVALUATION_DATASET_SCHEMA_VERSION = 2
TRANSFORM_VERSION = "corporate-travel-derived-v2"
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_CASE_FILE_BYTES = 50 * 1024 * 1024
SHA256_PATTERN = r"^[a-f0-9]{64}$"
GIT_SHA_PATTERN = r"^[a-f0-9]{40}$"

_UNSUPPORTED_CONSTRAINT_MARKERS = (
    "unsupported hard constraint",
    "unsupported soft preference",
    "unsupported constraint",
    "constraint is not supported",
    "\u4e0d\u652f\u6301",
)

WorkflowScenario = Literal[
    "COMPLIANT",
    "PREFERENCE_CONFLICT",
    "REQUIRES_APPROVAL",
    "NO_FEASIBLE_OPTION",
    "REVALIDATION_CHANGED",
    "PROVIDER_FAILURE",
]
IntentClassificationLabel = Literal[
    "TRIP",
    "TRANSPORT_COMPARE",
    "MULTI_DAY_TRIP",
    "NEEDS_CLARIFICATION",
    "OUT_OF_SCOPE",
]
IntentScenario = Literal[
    "LEGACY_UNSPECIFIED",
    "TRANSPORT_COMPARE",
    "MULTI_DAY_TRIP",
    "MISSING_FIELDS",
    "LOCAL_SEARCH",
    "ROUTE_PLANNING",
    "ONE_DAY_ITINERARY",
    "PREFERENCE_RICH_TRIP",
]
IntentCohort = Literal["LEGACY_TARGETED", "CURATED_FULL_SPLIT"]
FaultAction = Literal[
    "NONE",
    "SEARCH_FAILURE",
    "REVALIDATION_PRICE_CHANGED",
    "REVALIDATION_UNAVAILABLE",
    "REVALIDATION_FAILURE",
]
IntentField = Literal[
    "origin",
    "destination",
    "departure_after",
    "arrive_by",
    "return_after",
    "return_before",
    "hotel_check_in",
    "hotel_check_out",
]


class EvaluationDatasetError(RuntimeError):
    """评测数据集加载或校验失败。"""
    pass


class EvaluationModel(BaseModel):
    """评测数据集 Pydantic 模型基类。"""
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class SourceReference(EvaluationModel):
    """源数据/证据引用。"""
    dataset_id: Literal["UKPLab/PreferTripPlan", "Alibaba-NLP/Open-Travel"]
    source_split: str = Field(min_length=1, max_length=64)
    source_file: str = Field(min_length=1, max_length=256)
    source_record_id: str = Field(min_length=1, max_length=128)
    source_revision: str = Field(pattern=GIT_SHA_PATTERN)
    source_file_sha256: str = Field(pattern=SHA256_PATTERN)
    license: str = Field(min_length=1, max_length=64)
    original_query: str = Field(min_length=1, max_length=20_000)
    original_profile: str | None = Field(default=None, max_length=20_000)
    profile_drift: Literal["aligned", "omission", "inversion"] | None = None
    source_preferences: tuple[str, ...] = ()
    trip_context: str | None = Field(default=None, max_length=128)


class EmployeeFixture(EvaluationModel):
    """员工夹具。"""
    snapshot_id: str = Field(min_length=1, max_length=128)
    employee_id: str = Field(pattern=r"^SYN-E[0-9]{4}$")
    level: Literal["L3"]
    department: Literal["Evaluation"]
    home_city: str = Field(min_length=1, max_length=100)
    manager_id: str = Field(pattern=r"^SYN-M[0-9]{4}$")
    profile_version: Literal[1]


class PolicyFixture(EvaluationModel):
    """策略夹具。"""
    snapshot_id: str = Field(min_length=1, max_length=128)
    policy_version: Literal["derived-policy-v1"]
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    arrival_buffer_minutes: int = Field(ge=0, le=360)
    allowed_flight_classes: tuple[str, ...] = Field(min_length=1)
    allowed_train_classes: tuple[str, ...] = Field(min_length=1)
    hotel_city: str = Field(min_length=1, max_length=100)
    hotel_nightly_cap: Decimal = Field(gt=0)
    exception_allowed_rule_ids: tuple[str, ...]
    effective_from: date


class RequestFixture(EvaluationModel):
    """差旅请求夹具。"""
    origin: str = Field(min_length=1, max_length=100)
    destination: str = Field(min_length=1, max_length=100)
    departure_after: datetime
    arrive_by: datetime
    return_after: datetime
    return_before: datetime
    hotel_check_in: date
    hotel_check_out: date
    hard_constraints: tuple[HardConstraint, ...]
    soft_preferences: tuple[SoftPreference, ...]

    @model_validator(mode="after")
    def valid_request(self) -> RequestFixture:
        for name in ("departure_after", "arrive_by", "return_after", "return_before"):
            _require_aware(getattr(self, name), name)
        if self.origin.casefold() == self.destination.casefold():
            raise ValueError("origin and destination must differ")
        if self.arrive_by <= self.departure_after:
            raise ValueError("arrive_by must be later than departure_after")
        if self.return_before <= self.return_after:
            raise ValueError("return_before must be later than return_after")
        if self.hotel_check_out <= self.hotel_check_in:
            raise ValueError("hotel_check_out must be later than hotel_check_in")
        return self


class TransportFixture(EvaluationModel):
    """交通报价夹具。"""
    ref_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")
    direction: Literal["outbound", "inbound"]
    mode: TransportMode
    origin: str = Field(min_length=1, max_length=100)
    destination: str = Field(min_length=1, max_length=100)
    depart_at: datetime
    arrive_at: datetime
    price: Decimal = Field(gt=0)
    seat_class: str = Field(min_length=1, max_length=64)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    is_direct: bool = True
    available: bool

    @model_validator(mode="after")
    def valid_transport(self) -> TransportFixture:
        _require_aware(self.depart_at, "depart_at")
        _require_aware(self.arrive_at, "arrive_at")
        if self.arrive_at <= self.depart_at:
            raise ValueError("transport arrival must be later than departure")
        return self


class HotelFixture(EvaluationModel):
    """酒店报价夹具。"""
    ref_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,128}$")
    name: str = Field(min_length=1, max_length=200)
    city: str = Field(min_length=1, max_length=100)
    check_in: date
    check_out: date
    nightly_price: Decimal = Field(gt=0)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    commute_minutes: int = Field(ge=0, le=1_440)
    available: bool

    @model_validator(mode="after")
    def valid_stay(self) -> HotelFixture:
        if self.check_out <= self.check_in:
            raise ValueError("hotel checkout must be later than checkin")
        return self


class InventoryFixture(EvaluationModel):
    """库存夹具。"""
    provider: Literal["derived-mock"]
    source_type: Literal["MOCK"]
    captured_at: datetime
    valid_until: datetime
    transports: tuple[TransportFixture, ...] = Field(min_length=2)
    hotels: tuple[HotelFixture, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_inventory(self) -> InventoryFixture:
        _require_aware(self.captured_at, "captured_at")
        _require_aware(self.valid_until, "valid_until")
        if self.valid_until <= self.captured_at:
            raise ValueError("inventory valid_until must be later than captured_at")
        refs = [*(item.ref_id for item in self.transports), *(item.ref_id for item in self.hotels)]
        if len(refs) != len(set(refs)):
            raise ValueError("inventory references must be unique")
        directions = {item.direction for item in self.transports}
        if directions != {"outbound", "inbound"}:
            raise ValueError("inventory must contain outbound and inbound transport")
        return self


class FaultFixture(EvaluationModel):
    """故障注入夹具。"""
    action: FaultAction
    target_ref: str | None = Field(default=None, max_length=128)
    price_delta: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def valid_fault(self) -> FaultFixture:
        needs_target = self.action in {
            "REVALIDATION_PRICE_CHANGED",
            "REVALIDATION_UNAVAILABLE",
        }
        if needs_target != (self.target_ref is not None):
            raise ValueError("fault target_ref does not match action")
        needs_delta = self.action == "REVALIDATION_PRICE_CHANGED"
        if needs_delta != (self.price_delta is not None):
            raise ValueError("fault price_delta does not match action")
        return self


class ExpectedWorkflow(EvaluationModel):
    """期望工作流终态与结果。"""
    after_create_state: TaskState
    after_selection_state: TaskState | None
    selected_policy_outcome: PolicyOutcome | None
    selected_inventory_ref: str | None = Field(default=None, max_length=128)
    approval_required: bool
    booking_intent_expected: bool
    explicit_request_overrides_profile: bool


class WorkflowEvaluationCase(EvaluationModel):
    """工作流评测用例。"""
    case_id: str = Field(pattern=r"^prefer-[a-z0-9-]{1,100}$")
    scenario: WorkflowScenario
    source: SourceReference
    employee: EmployeeFixture
    policy: PolicyFixture
    request: RequestFixture
    inventory: InventoryFixture
    fault: FaultFixture
    expected: ExpectedWorkflow
    conversion_notes: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def consistent_case(self) -> WorkflowEvaluationCase:
        if self.source.dataset_id != "UKPLab/PreferTripPlan":
            raise ValueError("workflow cases must derive from PreferTripPlan")
        if self.employee.home_city != self.request.origin:
            raise ValueError("employee home city must match request origin")
        if self.policy.hotel_city != self.request.destination:
            raise ValueError("policy hotel city must match request destination")
        outbound = [
            item for item in self.inventory.transports if item.direction == "outbound"
        ]
        inbound = [item for item in self.inventory.transports if item.direction == "inbound"]
        if any(
            item.origin != self.request.origin
            or item.destination != self.request.destination
            for item in outbound
        ):
            raise ValueError("outbound fixture route does not match request")
        if any(
            item.origin != self.request.destination
            or item.destination != self.request.origin
            for item in inbound
        ):
            raise ValueError("inbound fixture route does not match request")
        if any(item.city != self.request.destination for item in self.inventory.hotels):
            raise ValueError("hotel fixture city does not match request")
        if self.expected.selected_inventory_ref is not None:
            refs = {
                *(item.ref_id for item in self.inventory.transports),
                *(item.ref_id for item in self.inventory.hotels),
            }
            if self.expected.selected_inventory_ref not in refs:
                raise ValueError("expected selected inventory reference is unavailable")
        return self


class ExpectedIntent(EvaluationModel):
    """期望意图抽取结果。"""
    classification: IntentClassificationLabel
    missing_fields: tuple[IntentField, ...] | None
    expected_transport_preferences: (
        tuple[
            Literal["prefer_train", "prefer_flight", "compare_train_and_flight"],
            ...,
        ]
        | None
    )
    must_clarify_before_search: bool
    must_not_invent_inventory: Literal[True]
    must_reject_unsupported_constraints: bool = False
    unsupported_constraint_categories: tuple[str, ...] = ()

    @model_validator(mode="after")
    def consistent_expectation(self) -> ExpectedIntent:
        if self.must_reject_unsupported_constraints != bool(
            self.unsupported_constraint_categories
        ):
            raise ValueError(
                "unsupported constraint categories must match the rejection expectation"
            )
        return self


class IntentEvaluationCase(EvaluationModel):
    """意图评测用例。"""
    case_id: str = Field(pattern=r"^(open|prefer)-[a-z0-9-]{1,100}$")
    scenario: IntentScenario = "LEGACY_UNSPECIFIED"
    cohort: IntentCohort = "LEGACY_TARGETED"
    language: Literal["en", "zh"] = "zh"
    source: SourceReference
    message: str = Field(min_length=1, max_length=20_000)
    expected: ExpectedIntent
    label_basis: str = Field(
        default="Legacy V1 label retained for backward compatibility",
        min_length=1,
        max_length=1_000,
    )
    conversion_notes: tuple[str, ...] = (
        "Legacy V1 case loaded through the backward-compatible V2 schema.",
    )

    @model_validator(mode="after")
    def consistent_case(self) -> IntentEvaluationCase:
        if self.message != self.source.original_query:
            raise ValueError("intent message must preserve the source query")
        if self.source.dataset_id == "UKPLab/PreferTripPlan" and self.language != "en":
            raise ValueError("PreferTripPlan intent cases must be labelled as English")
        if self.source.dataset_id == "Alibaba-NLP/Open-Travel" and self.language != "zh":
            raise ValueError("Open-Travel intent cases must be labelled as Chinese")
        if (
            self.expected.must_clarify_before_search
            and self.expected.missing_fields == ()
            and not self.expected.must_reject_unsupported_constraints
        ):
            raise ValueError(
                "pre-search clarification must identify missing fields or unsupported constraints"
            )
        if (
            self.expected.classification == "NEEDS_CLARIFICATION"
            and not self.expected.must_clarify_before_search
        ):
            raise ValueError("clarification cases must require pre-search clarification")
        if (
            self.expected.classification == "OUT_OF_SCOPE"
            and self.expected.must_clarify_before_search
        ):
            raise ValueError("out-of-scope cases must not enter trip clarification")
        return self


class SourceDatasetMetadata(EvaluationModel):
    """源数据集元数据。"""
    dataset_id: Literal["UKPLab/PreferTripPlan", "Alibaba-NLP/Open-Travel"]
    revision: str = Field(pattern=GIT_SHA_PATTERN)
    url: str = Field(pattern=r"^https://huggingface\.co/datasets/")
    license: str = Field(min_length=1, max_length=64)
    selected_records: int = Field(gt=0)
    use: str = Field(min_length=1, max_length=500)


class CaseFileMetadata(EvaluationModel):
    """用例文件元数据。"""
    path: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}\.jsonl$")
    sha256: str = Field(pattern=SHA256_PATTERN)
    records: int = Field(gt=0)


class EvaluationDatasetManifest(EvaluationModel):
    """评测数据集清单。"""
    schema_version: Literal[1, 2]
    dataset_id: Literal["corporate-travel-derived-v1", "corporate-travel-derived-v2"]
    dataset_version: Literal["1", "2"]
    classification: Literal["SYNTHETIC_DERIVED"]
    transform_version: Literal[
        "corporate-travel-derived-v1", "corporate-travel-derived-v2"
    ]
    created_at: datetime
    sources: tuple[SourceDatasetMetadata, SourceDatasetMetadata]
    workflow_cases: CaseFileMetadata
    intent_cases: CaseFileMetadata
    workflow_scenarios: dict[str, int]
    intent_scenarios: dict[str, int]
    intent_cohorts: dict[str, int] = {}
    intent_languages: dict[str, int] = {}
    real_provider_snapshots: Literal[0]
    limitations: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def consistent_manifest(self) -> EvaluationDatasetManifest:
        _require_aware(self.created_at, "created_at")
        expected_identity = (
            f"corporate-travel-derived-v{self.schema_version}",
            str(self.schema_version),
            f"corporate-travel-derived-v{self.schema_version}",
        )
        if (
            self.dataset_id,
            self.dataset_version,
            self.transform_version,
        ) != expected_identity:
            raise ValueError("manifest schema, dataset, and transform versions disagree")
        source_ids = {item.dataset_id for item in self.sources}
        if source_ids != {"UKPLab/PreferTripPlan", "Alibaba-NLP/Open-Travel"}:
            raise ValueError("manifest must identify both source datasets")
        if sum(self.workflow_scenarios.values()) != self.workflow_cases.records:
            raise ValueError("workflow scenario counts do not match record count")
        if sum(self.intent_scenarios.values()) != self.intent_cases.records:
            raise ValueError("intent scenario counts do not match record count")
        if self.intent_cohorts and sum(self.intent_cohorts.values()) != self.intent_cases.records:
            raise ValueError("intent cohort counts do not match record count")
        if (
            self.intent_languages
            and sum(self.intent_languages.values()) != self.intent_cases.records
        ):
            raise ValueError("intent language counts do not match record count")
        return self


@dataclass(frozen=True, slots=True)
class LoadedEvaluationDataset:
    """已加载的评测数据集。"""
    root: Path
    manifest: EvaluationDatasetManifest
    workflow_cases: tuple[WorkflowEvaluationCase, ...]
    intent_cases: tuple[IntentEvaluationCase, ...]
    manifest_sha256: str

    @property
    def total_cases(self) -> int:
        return len(self.workflow_cases) + len(self.intent_cases)


@dataclass(frozen=True, slots=True)
class WorkflowEvaluationObservation:
    """工作流用例运行观察。"""
    case_id: str
    after_create_state: TaskState
    after_selection_state: TaskState | None
    selected_policy_outcome: PolicyOutcome | None
    selected_inventory_refs: tuple[str, ...]
    booking_intent_created: bool
    final_state: TaskState
    failure_reason: str | None
    tool_calls_used: int
    task: TripTask


@dataclass(frozen=True, slots=True)
class IntentEvaluationObservation:
    """意图用例运行观察。"""
    case_id: str
    entrypoint: str
    expected_classification: str
    actual_classification: str
    expected_missing_fields: tuple[str, ...] | None
    actual_missing_fields: tuple[str, ...]
    clarification_expected: bool
    clarification_observed: bool
    expected_transport_preferences: tuple[str, ...] | None
    actual_transport_preferences: tuple[str, ...]
    unsupported_constraint_rejection_expected: bool
    unsupported_constraints_rejected: bool
    actual_conflicts: tuple[str, ...]
    provider_calls_before_clarification: int
    inventory_hallucinated: bool
    #: 任务最后停在哪个状态。三条入口对"越界""没货"的表达不同，比对时要看得见。
    final_state: str = ""


@dataclass(frozen=True, slots=True)
class IntentEvaluationMetrics:
    """意图评测聚合指标。"""
    entrypoint: str
    classification_status: str
    #: 缺失字段指标在这条入口上有没有暴露面。工具循环没有"必填表"，一句追问背后
    #: 没有字段名可读，按协议 §1.5 记 not_applicable，不记 0。
    missing_field_status: str
    #: 偏好指标的暴露面。工具循环里偏好只在交付时声明，停在追问的用例上读不到；
    #: 这条入口只对真的交付了方案的用例打分，一条都没交付就是 not_applicable。
    transport_preference_status: str
    total_cases: int
    scored_missing_field_cases: int
    scored_transport_preference_cases: int
    unsupported_constraint_cases: int
    classification_accuracy: float
    missing_field_exact_match_rate: float | None
    missing_field_precision: float | None
    missing_field_recall: float | None
    clarification_accuracy: float
    out_of_scope_accuracy: float | None
    transport_preference_accuracy: float | None
    unsupported_constraint_rejection_rate: float | None
    premature_provider_call_rate: float
    inventory_hallucination_rate: float
    observations: tuple[IntentEvaluationObservation, ...]


_WORKFLOW_ADAPTER = TypeAdapter(WorkflowEvaluationCase)
_INTENT_ADAPTER = TypeAdapter(IntentEvaluationCase)


def load_evaluation_dataset(directory: str | Path) -> LoadedEvaluationDataset:
    """从目录加载并校验评测数据集。"""
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise EvaluationDatasetError(f"Evaluation dataset directory does not exist: {root}")
    manifest_path = _safe_file(root, "manifest.json")
    manifest_bytes = _read_limited(manifest_path, MAX_MANIFEST_BYTES)
    try:
        manifest = EvaluationDatasetManifest.model_validate_json(manifest_bytes)
    except Exception as exc:
        raise EvaluationDatasetError(f"Invalid evaluation manifest: {exc}") from exc

    workflow_cases = _load_jsonl(
        root,
        manifest.workflow_cases,
        _WORKFLOW_ADAPTER,
    )
    intent_cases = _load_jsonl(root, manifest.intent_cases, _INTENT_ADAPTER)
    _require_unique_case_ids((*workflow_cases, *intent_cases))

    actual_workflow_counts = _counts(item.scenario for item in workflow_cases)
    actual_intent_counts = _counts(
        item.expected.classification if manifest.schema_version == 1 else item.scenario
        for item in intent_cases
    )
    if actual_workflow_counts != manifest.workflow_scenarios:
        raise EvaluationDatasetError("Workflow scenario counts do not match manifest")
    if actual_intent_counts != manifest.intent_scenarios:
        raise EvaluationDatasetError("Intent scenario counts do not match manifest")
    if manifest.intent_cohorts and _counts(
        item.cohort for item in intent_cases
    ) != manifest.intent_cohorts:
        raise EvaluationDatasetError("Intent cohort counts do not match manifest")
    if manifest.intent_languages and _counts(
        item.language for item in intent_cases
    ) != manifest.intent_languages:
        raise EvaluationDatasetError("Intent language counts do not match manifest")
    selected_by_source = _counts(
        item.source.dataset_id for item in (*workflow_cases, *intent_cases)
    )
    for source in manifest.sources:
        if selected_by_source.get(source.dataset_id, 0) != source.selected_records:
            raise EvaluationDatasetError(
                f"Selected count for {source.dataset_id} does not match manifest"
            )

    return LoadedEvaluationDataset(
        root=root,
        manifest=manifest,
        workflow_cases=workflow_cases,
        intent_cases=intent_cases,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )


WORKFLOW_MESSAGE_TEMPLATE_VERSION = "workflow-case-message-v1"

_HARD_CONSTRAINT_PHRASES: dict[str, str] = {
    "arrive_before_meeting": "I have to be there before my meeting starts",
    "hotel_required": "I also need a hotel booked for the nights in between",
    "direct_only": "the trip has to be a direct connection",
    "train_only": "please keep me on trains only",
    "flight_only": "please keep me on flights only",
}

_SOFT_PREFERENCE_PHRASES: dict[str, str] = {
    "lowest_cost": "keep the total as cheap as you can",
    "shortest_duration": "keep the travel time as short as you can",
    "avoid_early_departure": "avoid very early departures if possible",
    "hotel_near_client": "a hotel close to the client office would be better",
    "prefer_train": "I would rather take the train",
    "prefer_flight": "I would rather fly",
    "compare_train_and_flight": "show me both train and flight options",
}


def render_workflow_case_message(case: WorkflowEvaluationCase) -> str:
    """将工作流用例渲染为用户自然语言消息。"""

    request = case.request
    schedule = (
        f"I need to book a corporate trip from {request.origin} to "
        f"{request.destination}. I cannot leave before {_instant(request.departure_after)}"
    )
    if request.arrive_by is not None:
        schedule += f" and I must land by {_instant(request.arrive_by)}"
    schedule += "."
    if request.return_after is not None or request.return_before is not None:
        parts = []
        if request.return_after is not None:
            parts.append(f"no earlier than {_instant(request.return_after)}")
        if request.return_before is not None:
            parts.append(f"back by {_instant(request.return_before)}")
        schedule += f" Coming home {' and '.join(parts)}."
    if request.hotel_check_in is not None and request.hotel_check_out is not None:
        schedule += (
            f" The hotel runs {request.hotel_check_in.isoformat()} to "
            f"{request.hotel_check_out.isoformat()}."
        )
    clauses = [
        _HARD_CONSTRAINT_PHRASES[item]
        for item in request.hard_constraints
        if item in _HARD_CONSTRAINT_PHRASES
    ]
    clauses += [
        _SOFT_PREFERENCE_PHRASES[item]
        for item in request.soft_preferences
        if item in _SOFT_PREFERENCE_PHRASES
    ]
    if clauses:
        schedule += f" {_join_clauses(clauses)}."
    return schedule


def _instant(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _join_clauses(clauses: list[str]) -> str:
    sentence = clauses[0][0].upper() + clauses[0][1:]
    if len(clauses) == 1:
        return sentence
    return f"{sentence}, {', '.join(clauses[1:-1] + [f'and {clauses[-1]}'])}"


def run_workflow_evaluation_case(
    case: WorkflowEvaluationCase,
    *,
    trace_observer: WorkflowTraceObserverPort | None = None,
    tool_calling_language_model: object | None = None,
) -> WorkflowEvaluationObservation:
    """确定性执行单条工作流评测用例。

    不传模型就用结构化请求；传 ``tool_calling_language_model`` 就走产品入口（工具循环），
    冻结的结构化请求不进循环，只用来事后打分。
    """
    def clock() -> datetime:
        return case.inventory.captured_at

    transports = [
        TransportOffer(
            ref_id=item.ref_id,
            snapshot_id="derived-catalog",
            provider=case.inventory.provider,
            mode=item.mode,
            origin=item.origin,
            destination=item.destination,
            depart_at=item.depart_at,
            arrive_at=item.arrive_at,
            price=item.price,
            seat_class=item.seat_class,
            currency=item.currency,
            is_direct=item.is_direct,
            available=item.available,
        )
        for item in case.inventory.transports
    ]
    hotels = [
        HotelOffer(
            ref_id=item.ref_id,
            snapshot_id="derived-catalog",
            provider=case.inventory.provider,
            name=item.name,
            city=item.city,
            check_in=item.check_in,
            check_out=item.check_out,
            nightly_price=item.nightly_price,
            currency=item.currency,
            commute_minutes=item.commute_minutes,
            available=item.available,
        )
        for item in case.inventory.hotels
    ]
    provider = MockProvider(transports, hotels, clock=clock)
    if case.fault.action == "SEARCH_FAILURE":
        provider.fail_search = True

    employee = EmployeeProfileSnapshot(
        snapshot_id=case.employee.snapshot_id,
        employee_id=case.employee.employee_id,
        level=case.employee.level,
        department=case.employee.department,
        home_city=case.employee.home_city,
        manager_id=case.employee.manager_id,
        profile_version=case.employee.profile_version,
    )
    policy = PolicySnapshot(
        snapshot_id=case.policy.snapshot_id,
        policy_version=case.policy.policy_version,
        level_rules={
            case.employee.level: LevelTravelRule(
                allowed_flight_classes=case.policy.allowed_flight_classes,
                allowed_train_classes=case.policy.allowed_train_classes,
            )
        },
        hotel_city_caps={case.policy.hotel_city: case.policy.hotel_nightly_cap},
        arrival_buffer_minutes=case.policy.arrival_buffer_minutes,
        exception_allowed_rule_ids=frozenset(case.policy.exception_allowed_rule_ids),
        effective_from=case.policy.effective_from,
        currency=case.policy.currency,
    )
    request = TripRequestVersion(
        task_id=case.case_id,
        version=1,
        traveler_id=case.employee.employee_id,
        origin=case.request.origin,
        destination=case.request.destination,
        departure_after=case.request.departure_after,
        arrive_by=case.request.arrive_by,
        return_after=case.request.return_after,
        return_before=case.request.return_before,
        hotel_check_in=case.request.hotel_check_in,
        hotel_check_out=case.request.hotel_check_out,
        hard_constraints=case.request.hard_constraints,
        soft_preferences=case.request.soft_preferences,
        created_at=case.inventory.captured_at,
    )
    workflow = TripWorkflowOrchestrator(
        tasks=InMemoryTaskRepository(),
        employees=InMemoryEmployeeDirectory([employee]),
        policies=InMemoryPolicyRepository(policy),
        provider=provider,
        clock=clock,
        trace_observer=trace_observer,
        tool_calling_language_model=tool_calling_language_model,
    )
    if tool_calling_language_model is None:
        task = workflow.create_task(request)
    else:
        # 产品入口：模型每轮决定查什么，冻结的结构化请求不进循环。
        task = workflow.create_task_from_agentic_message(
            render_workflow_case_message(case),
            traveler_id=case.employee.employee_id,
            task_id=case.case_id,
        )
    after_create = task.state
    after_selection: TaskState | None = None
    selected_outcome: PolicyOutcome | None = None
    selected_refs: tuple[str, ...] = ()

    if task.state is TaskState.WAITING_FOR_USER:
        option = _select_expected_option(case, task.options)
        selected_outcome = option.policy_decision.outcome
        selected_refs = option.inventory_refs
        if case.fault.action == "REVALIDATION_PRICE_CHANGED":
            provider.price_overrides[case.fault.target_ref or ""] = _original_price(
                transports,
                hotels,
                case.fault.target_ref or "",
            ) + (case.fault.price_delta or Decimal("0"))
        elif case.fault.action == "REVALIDATION_UNAVAILABLE":
            provider.unavailable_refs.add(case.fault.target_ref or "")
        elif case.fault.action == "REVALIDATION_FAILURE":
            provider.fail_revalidation = True

        task = workflow.select_option(
            task.task_id,
            option.option_id,
            business_reason=(
                "Synthetic evaluation exception reason"
                if selected_outcome is PolicyOutcome.REQUIRES_APPROVAL
                else None
            ),
        )
        after_selection = task.state

    return WorkflowEvaluationObservation(
        case_id=case.case_id,
        after_create_state=after_create,
        after_selection_state=after_selection,
        selected_policy_outcome=selected_outcome,
        selected_inventory_refs=selected_refs,
        booking_intent_created=task.booking_intent is not None,
        final_state=task.state,
        failure_reason=task.failure,
        tool_calls_used=task.tool_calls_used,
        task=task,
    )


def run_intent_evaluation(
    cases: tuple[IntentEvaluationCase, ...],
    *,
    language_model=None,
    reference_time: datetime | None = None,
    entrypoint: str = "agentic",
) -> IntentEvaluationMetrics:
    """对意图用例执行产品入口（工具循环）并产出观察。

    只剩一条自然语言入口。``entrypoint`` 参数保留是为了让报告里的 ``entrypoint``
    字段和历史报告可对照；传别的值直接报错。``language_model`` 不传就用
    ``DeterministicToolCallingModel`` 替身。
    """
    from corporate_travel_agent.agent.deterministic_tool_model import (
        DeterministicToolCallingModel,
    )
    from corporate_travel_agent.demo import build_demo_system

    if entrypoint != "agentic":
        raise EvaluationDatasetError(f"Unsupported intent entrypoint: {entrypoint}")
    parser = language_model or DeterministicToolCallingModel()
    observed_at = reference_time or datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
    observations: list[IntentEvaluationObservation] = []

    for case in cases:
        workflow, _ = build_demo_system(
            tool_calling_language_model=parser,
            clock=_fixed_clock(observed_at),
        )
        task = workflow.create_task_from_agentic_message(
            case.message,
            traveler_id="E1001",
            task_id=f"intent-eval-{case.case_id}",
        )
        actual_missing = tuple(task.missing_required_fields)
        provider_calls = sum(
            record.tool_kind == "PROVIDER" for record in task.tool_calls
        )
        # 工具循环没有意图字段表；偏好只有在真的交付了方案之后才落在请求上。
        preferences = tuple(
            task.request.soft_preferences if task.request is not None else ()
        )
        # 不支持的要求在工具循环里只能出现在给旅行者的那句话里。
        rejection_texts = [
            *task.intent_conflicts,
            *([task.clarification_question] if task.clarification_question else []),
        ]
        observations.append(
            IntentEvaluationObservation(
                case_id=case.case_id,
                entrypoint=entrypoint,
                expected_classification=case.expected.classification,
                # 工具循环没有分类器；这一格永远是 UNCLASSIFIED，对应指标记 not_applicable。
                actual_classification="UNCLASSIFIED",
                expected_missing_fields=case.expected.missing_fields,
                actual_missing_fields=actual_missing,
                clarification_expected=case.expected.must_clarify_before_search,
                clarification_observed=task.state is TaskState.NEEDS_CLARIFICATION,
                expected_transport_preferences=case.expected.expected_transport_preferences,
                actual_transport_preferences=preferences,
                unsupported_constraint_rejection_expected=(
                    case.expected.must_reject_unsupported_constraints
                ),
                unsupported_constraints_rejected=any(
                    marker in text.casefold()
                    for text in rejection_texts
                    for marker in _UNSUPPORTED_CONSTRAINT_MARKERS
                ),
                actual_conflicts=tuple(task.intent_conflicts),
                provider_calls_before_clarification=provider_calls,
                inventory_hallucinated=bool(
                    task.options or task.selected_option_id or task.booking_intent
                ),
                final_state=task.state.value,
            )
        )

    return summarize_intent_observations(tuple(observations))


def summarize_intent_observations(
    observations: tuple[IntentEvaluationObservation, ...],
) -> IntentEvaluationMetrics:
    """汇总意图观察为指标。"""
    total = len(observations)
    if total == 0:
        raise EvaluationDatasetError("Intent evaluation requires at least one case")
    out_of_scope = [
        item for item in observations if item.expected_classification == "OUT_OF_SCOPE"
    ]
    scored_missing = [
        item for item in observations if item.expected_missing_fields is not None
    ]
    scored_transport_preferences = [
        item
        for item in observations
        if item.expected_transport_preferences is not None
    ]
    entrypoints = {item.entrypoint for item in observations}
    if len(entrypoints) != 1:
        raise EvaluationDatasetError(
            "Intent observations from different entrypoints must not be merged"
        )
    entrypoint = entrypoints.pop()
    if entrypoint == "agentic":
        # 工具循环里偏好落在交付出来的请求上；停在追问的用例什么都读不到，不算分母。
        scored_transport_preferences = [
            item for item in scored_transport_preferences if not item.clarification_observed
        ]
    unsupported_constraints = [
        item
        for item in observations
        if item.unsupported_constraint_rejection_expected
    ]
    true_positive_missing = sum(
        len(set(item.actual_missing_fields) & set(item.expected_missing_fields or ()))
        for item in scored_missing
    )
    predicted_missing = sum(
        len(set(item.actual_missing_fields)) for item in scored_missing
    )
    expected_missing = sum(
        len(set(item.expected_missing_fields or ())) for item in scored_missing
    )
    # 工具循环没有"必填表"：一句追问背后没有字段名可读。缺失字段那三个指标在这条
    # 入口上没有暴露面，按协议 §1.5 记 not_applicable，分母也不去凑。
    slots_exposed = entrypoint != "agentic"
    return IntentEvaluationMetrics(
        entrypoint=entrypoint,
        # 产品入口没有场景分类器（ADR-0002 删了旧分类器，ADR-0003 删了旧入口），
        # 该指标按协议 §1.5 记为 not_applicable，而不是记 0。
        classification_status="not_applicable",
        missing_field_status="measured" if slots_exposed else "not_applicable",
        transport_preference_status=(
            "measured" if scored_transport_preferences else "not_applicable"
        ),
        total_cases=total,
        scored_missing_field_cases=len(scored_missing),
        scored_transport_preference_cases=len(scored_transport_preferences),
        unsupported_constraint_cases=len(unsupported_constraints),
        classification_accuracy=_rate(
            item.actual_classification == item.expected_classification
            for item in observations
        ),
        missing_field_exact_match_rate=(
            _optional_rate(
                set(item.actual_missing_fields) == set(item.expected_missing_fields or ())
                for item in scored_missing
            )
            if slots_exposed
            else None
        ),
        missing_field_precision=(
            (
                true_positive_missing / predicted_missing
                if scored_missing and predicted_missing
                else 1.0
                if scored_missing
                else None
            )
            if slots_exposed
            else None
        ),
        missing_field_recall=(
            (
                true_positive_missing / expected_missing
                if scored_missing and expected_missing
                else 1.0
                if scored_missing
                else None
            )
            if slots_exposed
            else None
        ),
        clarification_accuracy=_rate(
            item.clarification_observed == item.clarification_expected
            for item in observations
        ),
        # 工具循环没有分类器，连 OUT_OF_SCOPE 这个标签都不产出——它只会开口说
        # "这不在差旅范围内"。没有暴露面的指标记 None，不记 0。
        out_of_scope_accuracy=(
            _optional_rate(
                item.actual_classification == "OUT_OF_SCOPE" for item in out_of_scope
            )
            if entrypoint != "agentic"
            else None
        ),
        transport_preference_accuracy=_optional_rate(
            set(item.actual_transport_preferences)
            == set(item.expected_transport_preferences or ())
            for item in scored_transport_preferences
        ),
        unsupported_constraint_rejection_rate=_optional_rate(
            item.unsupported_constraints_rejected for item in unsupported_constraints
        ),
        premature_provider_call_rate=_rate(
            item.provider_calls_before_clarification > 0 for item in observations
        ),
        inventory_hallucination_rate=_rate(
            item.inventory_hallucinated for item in observations
        ),
        observations=tuple(observations),
    )


def _rate(values) -> float:
    values = tuple(values)
    if not values:
        raise ValueError("A required rate cannot be calculated from an empty sequence")
    return sum(values) / len(values)


def _optional_rate(values) -> float | None:
    values = tuple(values)
    return sum(values) / len(values) if values else None


def _fixed_clock(at: datetime) -> Callable[[], datetime]:
    """评测用的钉死时钟：每条用例按自己的观察时刻跑。"""
    return lambda: at


def _select_expected_option(
    case: WorkflowEvaluationCase, options: list[TravelOptionVersion]
) -> TravelOptionVersion:
    for option in options:
        if (
            case.expected.selected_inventory_ref is not None
            and case.expected.selected_inventory_ref not in option.inventory_refs
        ):
            continue
        if (
            case.expected.selected_policy_outcome is not None
            and option.policy_decision.outcome
            is not case.expected.selected_policy_outcome
        ):
            continue
        return option
    raise EvaluationDatasetError(f"No expected option exists for {case.case_id}")


def _original_price(
    transports: list[TransportOffer],
    hotels: list[HotelOffer],
    ref_id: str,
) -> Decimal:
    for item in transports:
        if item.ref_id == ref_id:
            return item.price
    for hotel in hotels:
        if hotel.ref_id == ref_id:
            return hotel.nightly_price
    raise EvaluationDatasetError(f"Unknown inventory reference: {ref_id}")


def _load_jsonl(root: Path, metadata: CaseFileMetadata, adapter: TypeAdapter) -> tuple:
    path = _safe_file(root, metadata.path)
    content = _read_limited(path, MAX_CASE_FILE_BYTES)
    if hashlib.sha256(content).hexdigest() != metadata.sha256:
        raise EvaluationDatasetError(f"Case file hash mismatch: {metadata.path}")
    records = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            raise EvaluationDatasetError(
                f"Blank line in {metadata.path} at line {line_number}"
            )
        try:
            records.append(adapter.validate_json(line))
        except Exception as exc:
            raise EvaluationDatasetError(
                f"Invalid record in {metadata.path} at line {line_number}: {exc}"
            ) from exc
    if len(records) != metadata.records:
        raise EvaluationDatasetError(f"Record count mismatch: {metadata.path}")
    return tuple(records)


def _safe_file(root: Path, relative_name: str) -> Path:
    relative = Path(relative_name)
    if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 1:
        raise EvaluationDatasetError(f"Unsafe evaluation dataset path: {relative_name}")
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise EvaluationDatasetError(f"Evaluation dataset file is unavailable: {relative_name}")
    return path


def _read_limited(path: Path, limit: int) -> bytes:
    if path.stat().st_size > limit:
        raise EvaluationDatasetError(f"Evaluation dataset file exceeds {limit} bytes")
    return path.read_bytes()


def _require_unique_case_ids(cases: tuple) -> None:
    case_ids = [item.case_id for item in cases]
    if len(case_ids) != len(set(case_ids)):
        raise EvaluationDatasetError("Evaluation dataset contains duplicate case IDs")


def _counts(values) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        key = value.value if hasattr(value, "value") else str(value)
        result[key] = result.get(key, 0) + 1
    return dict(sorted(result.items()))


def canonical_json_bytes(value: object) -> bytes:
    """生成稳定排序的规范 JSON 字节。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
