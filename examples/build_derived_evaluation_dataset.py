from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter, defaultdict, deque
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from corporate_travel_agent.domain.enums import PolicyOutcome, TaskState, TransportMode
from corporate_travel_agent.services.evaluation_dataset import (
    EVALUATION_DATASET_SCHEMA_VERSION,
    TRANSFORM_VERSION,
    CaseFileMetadata,
    EmployeeFixture,
    EvaluationDatasetManifest,
    ExpectedIntent,
    ExpectedWorkflow,
    FaultFixture,
    HotelFixture,
    IntentEvaluationCase,
    InventoryFixture,
    PolicyFixture,
    RequestFixture,
    SourceDatasetMetadata,
    SourceReference,
    TransportFixture,
    WorkflowEvaluationCase,
    canonical_json_bytes,
)

PREFER_REVISION = "cdc7e11d5d2a0fd2cd53f03973819e469bc004b9"
OPEN_REVISION = "b997227fb474eb578b7b951694fd4a6bf751bafa"
PREFER_FILE_SHA256 = "0918c014c9f571f30b9859e1402cac41e5fc139f33bc45d686b5fe15cdc2c28c"
OPEN_FILE_SHA256 = {
    "compare_itinerary.jsonl": (
        "12d3eae582ff7449753a1522042fd4023046680f4af1cf9f543f3c85f06d1866"
    ),
    "direction.jsonl": (
        "d417d58702bb4f67a37aae6bc81991b48a331f8f3167d62387fd58bfcee0f2fd"
    ),
    "multi_day_travel.jsonl": (
        "8755d45841f25001e9d744182968b2353c2339e2f41528351184ed1b19e4a04f"
    ),
    "one_day_travel.jsonl": (
        "19261c46535806ce564a0254cabb5f50cdb817546b0176b8ed0d6853f3e92b03"
    ),
    "search_around.jsonl": (
        "30080573c75e6809d06e2419ed0a5b8b713780c3c4cd3cda3bed15897fbb2e98"
    ),
    "train.jsonl": (
        "c317bba56df17bc08fa961bcd7066360037782420e954d0389d5fd028758a98d"
    ),
}
CREATED_AT = datetime(2026, 8, 2, 0, 0, tzinfo=UTC)
HOTEL_CAP = Decimal("600")

PREFERENCE_COUNT = 10
BASE_SCENARIOS = (
    ["COMPLIANT"] * 20
    + ["REQUIRES_APPROVAL"] * 10
    + ["NO_FEASIBLE_OPTION"] * 8
    + ["REVALIDATION_CHANGED"] * 6
    + ["PROVIDER_FAILURE"] * 6
)

INCOMPLETE_QUERIES = (
    (
        "人均1500，周末getaway，广州出发。",
        ("destination", "departure_after", "arrive_by"),
    ),
    (
        "突然多出三天假，广州出发能去哪？",
        ("destination", "departure_after", "arrive_by"),
    ),
    (
        "蜜月旅行，国内，预算3万，想玩得轻松又特别。",
        ("origin", "destination", "departure_after", "arrive_by"),
    ),
    (
        "寒假带上初中的孩子去研学游，哪个城市合适？",
        ("origin", "destination", "departure_after", "arrive_by"),
    ),
    (
        "小高老师我带着小孩要去杭州玩3天帮我制定一个详细的旅游计划",
        ("origin", "departure_after", "arrive_by"),
    ),
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build a deterministic 60+480 synthetic corporate-travel evaluation dataset "
            "from complete pinned test splits. No provider or language model is called."
        )
    )
    parser.add_argument("--prefertripplan-file", required=True)
    parser.add_argument("--open-travel-directory", required=True)
    parser.add_argument("--output-directory", required=True)
    args = parser.parse_args()

    prefer_path = Path(args.prefertripplan_file).expanduser().resolve()
    open_root = Path(args.open_travel_directory).expanduser().resolve()
    output = Path(args.output_directory).expanduser().resolve()
    if output.exists():
        raise SystemExit(f"Refusing to overwrite existing evaluation dataset: {output}")

    _require_hash(prefer_path, PREFER_FILE_SHA256)
    open_paths = {
        name: _find_open_file(open_root, name) for name in OPEN_FILE_SHA256
    }
    for name, path in open_paths.items():
        _require_hash(path, OPEN_FILE_SHA256[name])

    prefer_rows = _read_jsonl(prefer_path)
    workflow_cases = _build_workflow_cases(prefer_rows)
    intent_cases = _build_intent_cases(prefer_rows, open_paths)

    workflow_bytes = _jsonl_bytes(workflow_cases)
    intent_bytes = _jsonl_bytes(intent_cases)
    workflow_counts = dict(sorted(Counter(item.scenario for item in workflow_cases).items()))
    intent_counts = dict(
        sorted(Counter(item.scenario for item in intent_cases).items())
    )
    intent_cohorts = dict(sorted(Counter(item.cohort for item in intent_cases).items()))
    intent_languages = dict(
        sorted(Counter(item.language for item in intent_cases).items())
    )
    selected_by_source = Counter(
        item.source.dataset_id for item in (*workflow_cases, *intent_cases)
    )
    manifest = EvaluationDatasetManifest(
        schema_version=EVALUATION_DATASET_SCHEMA_VERSION,
        dataset_id="corporate-travel-derived-v2",
        dataset_version="2",
        classification="SYNTHETIC_DERIVED",
        transform_version=TRANSFORM_VERSION,
        created_at=CREATED_AT,
        sources=(
            SourceDatasetMetadata(
                dataset_id="UKPLab/PreferTripPlan",
                revision=PREFER_REVISION,
                url="https://huggingface.co/datasets/UKPLab/PreferTripPlan",
                license="Apache-2.0 + upstream TravelPlanner terms",
                selected_records=selected_by_source["UKPLab/PreferTripPlan"],
                use=(
                    "Structured workflow cases plus the complete 225-row curated test "
                    "split for English intent and unsupported-preference evaluation."
                ),
            ),
            SourceDatasetMetadata(
                dataset_id="Alibaba-NLP/Open-Travel",
                revision=OPEN_REVISION,
                url="https://huggingface.co/datasets/Alibaba-NLP/Open-Travel",
                license="CC-BY-NC-4.0",
                selected_records=selected_by_source["Alibaba-NLP/Open-Travel"],
                use=(
                    "Complete 250-row categorized test split plus five retained legacy "
                    "training queries for Chinese intent and scope-boundary evaluation."
                ),
            ),
        ),
        workflow_cases=CaseFileMetadata(
            path="workflow-cases.jsonl",
            sha256=hashlib.sha256(workflow_bytes).hexdigest(),
            records=len(workflow_cases),
        ),
        intent_cases=CaseFileMetadata(
            path="intent-cases.jsonl",
            sha256=hashlib.sha256(intent_bytes).hexdigest(),
            records=len(intent_cases),
        ),
        workflow_scenarios=workflow_counts,
        intent_scenarios=intent_counts,
        intent_cohorts=intent_cohorts,
        intent_languages=intent_languages,
        real_provider_snapshots=0,
        limitations=(
            "All inventory is deterministic MOCK data and must not count as a real snapshot.",
            "PreferTripPlan workflow party size is normalized to one employee.",
            "Intent cases preserve source text but do not use source answers as ground truth.",
            "Open-Travel final answers and tool traces are not used as ground truth.",
            "Employee profiles, policies, meeting windows, approvals, and faults are synthetic.",
        ),
    )

    output.mkdir(parents=True, mode=0o755)
    _write_new(output / "workflow-cases.jsonl", workflow_bytes)
    _write_new(output / "intent-cases.jsonl", intent_bytes)
    _write_new(
        output / "manifest.json",
        canonical_json_bytes(manifest.model_dump(mode="json")),
    )
    print(
        json.dumps(
            {
                "dataset_id": manifest.dataset_id,
                "workflow_cases": len(workflow_cases),
                "intent_cases": len(intent_cases),
                "total_cases": len(workflow_cases) + len(intent_cases),
                "workflow_scenarios": workflow_counts,
                "intent_scenarios": intent_counts,
                "intent_cohorts": intent_cohorts,
                "intent_languages": intent_languages,
                "real_provider_snapshots": 0,
                "output_directory": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _build_workflow_cases(rows: list[dict[str, Any]]) -> list[WorkflowEvaluationCase]:
    eligible = [row for row in rows if row.get("visiting_city_number") == 1]
    if len(eligible) < 60:
        raise ValueError("PreferTripPlan source has fewer than 60 single-city records")

    divergent = [
        row
        for row in eligible
        if row.get("profile_drift") != "aligned"
        and _mapped_source_preference(row) is not None
    ]
    preference_rows = _balanced_select(divergent, PREFERENCE_COUNT)
    preference_ids = {row["id"] for row in preference_rows}
    base_rows = _balanced_select(
        [row for row in eligible if row["id"] not in preference_ids],
        len(BASE_SCENARIOS),
    )
    assigned = [
        *(('PREFERENCE_CONFLICT', row) for row in preference_rows),
        *zip(BASE_SCENARIOS, base_rows, strict=True),
    ]
    return [
        _build_workflow_case(row, scenario, scenario_index=index)
        for index, (scenario, row) in enumerate(assigned)
    ]


def _balanced_select(rows: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(deque)
    for row in sorted(rows, key=lambda item: int(item["id"])):
        buckets[(str(row["level"]), str(row["profile_drift"]))].append(row)
    keys = sorted(buckets)
    selected: list[dict[str, Any]] = []
    while len(selected) < count:
        progress = False
        for key in keys:
            if buckets[key] and len(selected) < count:
                selected.append(buckets[key].popleft())
                progress = True
        if not progress:
            raise ValueError(f"Could not select {count} balanced PreferTripPlan rows")
    return selected


def _build_workflow_case(
    row: dict[str, Any],
    scenario: str,
    *,
    scenario_index: int,
) -> WorkflowEvaluationCase:
    source_id = int(row["source_id"])
    prefix = f"P{source_id:04d}"
    start_date = date.fromisoformat(row["date"][0])
    end_date = date.fromisoformat(row["date"][-1])
    captured_at = datetime.combine(start_date - timedelta(days=30), time(9), tzinfo=UTC)
    request = RequestFixture(
        origin=row["org"],
        destination=row["dest"],
        departure_after=datetime.combine(start_date, time(6), tzinfo=UTC),
        arrive_by=datetime.combine(start_date, time(13), tzinfo=UTC),
        return_after=datetime.combine(end_date, time(15), tzinfo=UTC),
        return_before=datetime.combine(end_date, time(23), tzinfo=UTC),
        hotel_check_in=start_date,
        hotel_check_out=end_date,
        hard_constraints=("arrive_before_meeting", "hotel_required"),
        soft_preferences=_soft_preferences(row, scenario),
    )
    inventory, fault, selected_ref = _build_inventory_and_fault(
        prefix,
        scenario,
        scenario_index,
        request,
        captured_at,
    )
    policy_outcome, after_create, after_selection, approval, booking = _expectations(
        scenario,
        fault.action,
    )
    source_preferences = tuple(
        item.get("preference", "")
        for item in json.loads(row["preferences_shorthand"])
        if item.get("preference")
    )
    trip_context = json.loads(row["trip_context"]).get("category")
    scenario_slug = scenario.casefold().replace("_", "-")
    return WorkflowEvaluationCase(
        case_id=f"prefer-{scenario_slug}-{source_id:04d}",
        scenario=scenario,
        source=SourceReference(
            dataset_id="UKPLab/PreferTripPlan",
            source_split="test",
            source_file="prefertripplan_test.jsonl",
            source_record_id=f"test:{row['id']}:source:{source_id}",
            source_revision=PREFER_REVISION,
            source_file_sha256=PREFER_FILE_SHA256,
            license="Apache-2.0 + upstream TravelPlanner terms",
            original_query=row["query"],
            original_profile=row["profile"],
            profile_drift=row["profile_drift"],
            source_preferences=source_preferences,
            trip_context=trip_context,
        ),
        employee=EmployeeFixture(
            snapshot_id=f"employee-{prefix}-v1",
            employee_id=f"SYN-E{source_id:04d}",
            level="L3",
            department="Evaluation",
            home_city=request.origin,
            manager_id=f"SYN-M{source_id:04d}",
            profile_version=1,
        ),
        policy=PolicyFixture(
            snapshot_id=f"policy-{prefix}-v1",
            policy_version="derived-policy-v1",
            arrival_buffer_minutes=60,
            allowed_flight_classes=("ECONOMY",),
            allowed_train_classes=("SECOND_CLASS",),
            hotel_city=request.destination,
            hotel_nightly_cap=HOTEL_CAP,
            exception_allowed_rule_ids=(
                "transport.flight.seat_class",
                "hotel.city.nightly_cap",
            ),
            effective_from=date(captured_at.year, 1, 1),
        ),
        request=request,
        inventory=inventory,
        fault=fault,
        expected=ExpectedWorkflow(
            after_create_state=after_create,
            after_selection_state=after_selection,
            selected_policy_outcome=policy_outcome,
            selected_inventory_ref=selected_ref,
            approval_required=approval,
            booking_intent_expected=booking,
            explicit_request_overrides_profile=scenario == "PREFERENCE_CONFLICT",
        ),
        conversion_notes=(
            "Source trip normalized to one synthetic employee and one corporate policy.",
            "Source geography and dates are retained for offline deterministic testing.",
            "Inventory, meeting windows, approval expectations, and faults are "
            "synthetic MOCK data.",
        ),
    )


def _soft_preferences(row: dict[str, Any], scenario: str) -> tuple[str, ...]:
    if scenario == "PREFERENCE_CONFLICT":
        mapped = _mapped_source_preference(row)
        if mapped is None:
            raise ValueError("Preference-conflict row lacks a supported source preference")
        return (mapped,)
    return ("lowest_cost",)


def _mapped_source_preference(row: dict[str, Any]) -> str | None:
    preferences = " ".join(
        item.get("preference", "")
        for item in json.loads(row["preferences_shorthand"])
    )
    if "Accommodation.cost ≤" in preferences or "min(Accommodation.cost)" in preferences:
        return "lowest_cost"
    return None


def _build_inventory_and_fault(
    prefix: str,
    scenario: str,
    index: int,
    request: RequestFixture,
    captured_at: datetime,
) -> tuple[InventoryFixture, FaultFixture, str | None]:
    outbound_ref = f"{prefix}-OUT-FLIGHT"
    inbound_ref = f"{prefix}-IN-FLIGHT"
    policy_hotel_ref = f"{prefix}-HT-POLICY"
    outbound_class = "ECONOMY"
    outbound_arrival = request.departure_after.replace(hour=10, minute=0)
    hotel_price = Decimal("520")
    hotel_available = True
    transports: list[TransportFixture] = []
    hotels: list[HotelFixture] = []
    selected_ref: str | None = outbound_ref
    fault = FaultFixture(action="NONE")

    if scenario == "REQUIRES_APPROVAL" and index % 2 == 0:
        outbound_class = "BUSINESS"
    if scenario == "REQUIRES_APPROVAL" and index % 2 == 1:
        hotel_price = Decimal("720")
    if scenario == "NO_FEASIBLE_OPTION" and index % 2 == 0:
        outbound_arrival = request.departure_after.replace(hour=12, minute=30)
        selected_ref = None
    if scenario == "NO_FEASIBLE_OPTION" and index % 2 == 1:
        hotel_available = False
        selected_ref = None

    transports.append(
        TransportFixture(
            ref_id=outbound_ref,
            direction="outbound",
            mode=TransportMode.FLIGHT,
            origin=request.origin,
            destination=request.destination,
            depart_at=request.departure_after.replace(hour=8, minute=0),
            arrive_at=outbound_arrival,
            price=Decimal("450"),
            seat_class=outbound_class,
            available=True,
        )
    )
    transports.append(
        TransportFixture(
            ref_id=inbound_ref,
            direction="inbound",
            mode=TransportMode.FLIGHT,
            origin=request.destination,
            destination=request.origin,
            depart_at=request.return_after.replace(hour=17, minute=0),
            arrive_at=request.return_after.replace(hour=19, minute=0),
            price=Decimal("350"),
            seat_class="ECONOMY",
            available=True,
        )
    )
    hotels.append(
        HotelFixture(
            ref_id=policy_hotel_ref,
            name="Derived Policy Hotel",
            city=request.destination,
            check_in=request.hotel_check_in,
            check_out=request.hotel_check_out,
            nightly_price=hotel_price,
            commute_minutes=42,
            available=hotel_available,
        )
    )

    if scenario == "PREFERENCE_CONFLICT":
        cheap_ref = f"{prefix}-HT-LOWEST-COST"
        hotels.append(
            HotelFixture(
                ref_id=cheap_ref,
                name="Derived Lowest Cost Hotel",
                city=request.destination,
                check_in=request.hotel_check_in,
                check_out=request.hotel_check_out,
                nightly_price=Decimal("480"),
                commute_minutes=42,
                available=True,
            )
        )
        selected_ref = cheap_ref

    if scenario == "REVALIDATION_CHANGED":
        fault = (
            FaultFixture(
                action="REVALIDATION_PRICE_CHANGED",
                target_ref=outbound_ref,
                price_delta=Decimal("100"),
            )
            if index % 2 == 0
            else FaultFixture(action="REVALIDATION_UNAVAILABLE", target_ref=outbound_ref)
        )
    elif scenario == "PROVIDER_FAILURE":
        fault = FaultFixture(
            action="SEARCH_FAILURE" if index % 2 == 0 else "REVALIDATION_FAILURE"
        )
        if fault.action == "SEARCH_FAILURE":
            selected_ref = None

    return (
        InventoryFixture(
            provider="derived-mock",
            source_type="MOCK",
            captured_at=captured_at,
            valid_until=captured_at + timedelta(minutes=15),
            transports=tuple(transports),
            hotels=tuple(hotels),
        ),
        fault,
        selected_ref,
    )


def _expectations(
    scenario: str,
    fault_action: str,
) -> tuple[PolicyOutcome | None, TaskState, TaskState | None, bool, bool]:
    if scenario == "NO_FEASIBLE_OPTION":
        return None, TaskState.NO_FEASIBLE_OPTION, None, False, False
    if fault_action == "SEARCH_FAILURE":
        return None, TaskState.PROVIDER_FAILED, None, False, False
    if scenario == "REQUIRES_APPROVAL":
        return (
            PolicyOutcome.REQUIRES_APPROVAL,
            TaskState.WAITING_FOR_USER,
            TaskState.WAITING_FOR_APPROVAL,
            True,
            False,
        )
    if scenario == "REVALIDATION_CHANGED":
        return (
            PolicyOutcome.COMPLIANT,
            TaskState.WAITING_FOR_USER,
            TaskState.RECONFIRMATION_REQUIRED,
            False,
            False,
        )
    if fault_action == "REVALIDATION_FAILURE":
        return (
            PolicyOutcome.COMPLIANT,
            TaskState.WAITING_FOR_USER,
            TaskState.PROVIDER_FAILED,
            False,
            False,
        )
    return (
        PolicyOutcome.COMPLIANT,
        TaskState.WAITING_FOR_USER,
        TaskState.READY_FOR_HANDOFF,
        False,
        True,
    )


def _build_intent_cases(
    prefer_rows: list[dict[str, Any]],
    paths: dict[str, Path],
) -> list[IntentEvaluationCase]:
    if len(prefer_rows) != 225:
        raise ValueError("PreferTripPlan curated test split must contain exactly 225 rows")

    open_test_files = (
        "compare_itinerary.jsonl",
        "direction.jsonl",
        "multi_day_travel.jsonl",
        "one_day_travel.jsonl",
        "search_around.jsonl",
    )
    open_rows = {name: _read_jsonl(paths[name]) for name in open_test_files}
    for name, rows in open_rows.items():
        if len(rows) != 50:
            raise ValueError(f"Open-Travel test file must contain 50 rows: {name}")

    legacy_keys: set[tuple[str, int]] = set()
    legacy_keys.update(
        ("compare_itinerary.jsonl", index)
        for index, row in enumerate(open_rows["compare_itinerary.jsonl"], start=1)
        if "高铁" in row["query"] and "飞机" in row["query"]
    )
    legacy_keys = set(sorted(legacy_keys)[:15])
    legacy_multi = [
        ("multi_day_travel.jsonl", index)
        for index, row in enumerate(open_rows["multi_day_travel.jsonl"], start=1)
        if any(keyword in row["query"] for keyword in ("住宿", "酒店", "住处"))
    ][:5]
    if len(legacy_multi) != 5:
        raise ValueError("Open-Travel multi-day split lacks five legacy hotel cases")
    legacy_keys.update(legacy_multi)
    legacy_keys.update(("search_around.jsonl", index) for index in range(1, 6))

    cases = [
        _build_open_test_intent_case(
            file_name=file_name,
            line_number=line_number,
            query=row["query"],
            legacy=(file_name, line_number) in legacy_keys,
        )
        for file_name in open_test_files
        for line_number, row in enumerate(open_rows[file_name], start=1)
    ]

    train_rows = _read_jsonl(paths["train.jsonl"])
    train_by_query = {
        row["query"]: (index, row)
        for index, row in enumerate(train_rows, start=1)
    }
    for query, missing_fields in INCOMPLETE_QUERIES:
        try:
            index, row = train_by_query[query]
        except KeyError as exc:
            raise ValueError(f"Pinned Open-Travel query is unavailable: {query}") from exc
        cases.append(
            _build_retained_training_intent_case(
                line_number=index,
                query=row["query"],
                missing_fields=missing_fields,
            )
        )

    cases.extend(
        _build_prefer_intent_case(row)
        for row in sorted(prefer_rows, key=lambda item: int(item["id"]))
    )
    if len(cases) != 480:
        raise ValueError(f"Expected 480 intent cases, generated {len(cases)}")
    return cases


def _build_open_test_intent_case(
    *,
    file_name: str,
    line_number: int,
    query: str,
    legacy: bool,
) -> IntentEvaluationCase:
    file_slug = file_name.removesuffix(".jsonl").replace("_", "-")
    scenario_by_file = {
        "compare_itinerary.jsonl": "TRANSPORT_COMPARE",
        "direction.jsonl": "ROUTE_PLANNING",
        "multi_day_travel.jsonl": "MULTI_DAY_TRIP",
        "one_day_travel.jsonl": "ONE_DAY_ITINERARY",
        "search_around.jsonl": "LOCAL_SEARCH",
    }
    scenario = scenario_by_file[file_name]
    out_of_scope = file_name in {
        "direction.jsonl",
        "one_day_travel.jsonl",
        "search_around.jsonl",
    }
    classification = (
        "OUT_OF_SCOPE"
        if out_of_scope
        else "TRANSPORT_COMPARE"
        if file_name == "compare_itinerary.jsonl"
        else "MULTI_DAY_TRIP"
    )
    missing_fields: tuple[str, ...] | None = None
    if legacy and file_name == "compare_itinerary.jsonl":
        missing_fields = ("arrive_by",)
    elif legacy and file_name == "multi_day_travel.jsonl":
        missing_fields = ("arrive_by", "return_after", "return_before")

    unsupported: list[str] = []
    if file_name == "compare_itinerary.jsonl" and any(
        keyword in query for keyword in ("自驾", "开车", "油费")
    ):
        unsupported.append("transport_mode:self_driving")
    if file_name == "multi_day_travel.jsonl":
        unsupported.append("planning_scope:leisure_itinerary")
        if "自驾" in query:
            unsupported.append("transport_mode:self_driving")

    return IntentEvaluationCase(
        case_id=f"open-test-{file_slug}-{line_number:04d}",
        scenario=scenario,
        cohort="LEGACY_TARGETED" if legacy else "CURATED_FULL_SPLIT",
        language="zh",
        source=SourceReference(
            dataset_id="Alibaba-NLP/Open-Travel",
            source_split=f"test/{file_slug}",
            source_file=file_name,
            source_record_id=f"test/{file_slug}:{line_number}",
            source_revision=OPEN_REVISION,
            source_file_sha256=OPEN_FILE_SHA256[file_name],
            license="CC-BY-NC-4.0",
            original_query=query,
        ),
        message=query,
        expected=ExpectedIntent(
            classification=classification,
            missing_fields=missing_fields,
            expected_transport_preferences=_open_transport_preferences(
                query,
                score=file_name in {"compare_itinerary.jsonl", "multi_day_travel.jsonl"},
            ),
            must_clarify_before_search=not out_of_scope,
            must_not_invent_inventory=True,
            must_reject_unsupported_constraints=bool(unsupported),
            unsupported_constraint_categories=tuple(unsupported),
        ),
        label_basis=(
            "Open-Travel test subtask supplies the intent/scope label. Transport signals "
            "come from explicit mode words. Exact missing-field gold is retained only for "
            "the previously reviewed legacy slice."
        ),
        conversion_notes=(
            "The original query is preserved verbatim.",
            "Source answers, tool traces, and reference payloads are not used as gold.",
        ),
    )


def _build_retained_training_intent_case(
    *,
    line_number: int,
    query: str,
    missing_fields: tuple[str, ...],
) -> IntentEvaluationCase:
    return IntentEvaluationCase(
        case_id=f"open-train-missing-fields-{line_number:04d}",
        scenario="MISSING_FIELDS",
        cohort="LEGACY_TARGETED",
        language="zh",
        source=SourceReference(
            dataset_id="Alibaba-NLP/Open-Travel",
            source_split="train",
            source_file="train.jsonl",
            source_record_id=f"train:{line_number}",
            source_revision=OPEN_REVISION,
            source_file_sha256=OPEN_FILE_SHA256["train.jsonl"],
            license="CC-BY-NC-4.0",
            original_query=query,
        ),
        message=query,
        expected=ExpectedIntent(
            classification="NEEDS_CLARIFICATION",
            missing_fields=missing_fields,
            expected_transport_preferences=None,
            must_clarify_before_search=True,
            must_not_invent_inventory=True,
            must_reject_unsupported_constraints=True,
            unsupported_constraint_categories=("planning_scope:leisure_itinerary",),
        ),
        label_basis=(
            "Five manually reviewed V1 training queries are retained only as a continuity "
            "slice; they do not contribute to the curated-test cohort."
        ),
        conversion_notes=(
            "The original query and V1 missing-field annotation are preserved.",
            "This case is reported separately from the complete test splits.",
        ),
    )


def _build_prefer_intent_case(row: dict[str, Any]) -> IntentEvaluationCase:
    preferences = json.loads(row["preferences_json"])
    preference_shorthand = tuple(
        item.get("preference", "")
        for item in json.loads(row["preferences_shorthand"])
        if item.get("preference")
    )
    local_constraints = json.loads(row["local_constraint"])
    unsupported = ["planning_scope:full_itinerary"]
    if int(row["visiting_city_number"]) > 1:
        unsupported.append("trip_shape:multi_city")
    unsupported.extend(
        f"local_constraint:{key.casefold().replace(' ', '_')}"
        for key, value in sorted(local_constraints.items())
        if value not in (None, "", [], {})
    )
    unsupported.extend(
        f"preference_paradigm:{str(item['paradigm']).casefold()}"
        for item in preferences
    )
    trip_context = json.loads(row["trip_context"]).get("category")
    source_id = int(row["source_id"])
    row_id = int(row["id"])
    return IntentEvaluationCase(
        case_id=f"prefer-intent-{row_id:04d}",
        scenario="PREFERENCE_RICH_TRIP",
        cohort="CURATED_FULL_SPLIT",
        language="en",
        source=SourceReference(
            dataset_id="UKPLab/PreferTripPlan",
            source_split="test",
            source_file="prefertripplan_test.jsonl",
            source_record_id=f"test:intent:{row_id}:source:{source_id}",
            source_revision=PREFER_REVISION,
            source_file_sha256=PREFER_FILE_SHA256,
            license="Apache-2.0 + upstream TravelPlanner terms",
            original_query=row["query"],
            original_profile=row["profile"],
            profile_drift=row["profile_drift"],
            source_preferences=preference_shorthand,
            trip_context=trip_context,
        ),
        message=row["query"],
        expected=ExpectedIntent(
            classification="MULTI_DAY_TRIP",
            missing_fields=("arrive_by", "return_after", "return_before"),
            expected_transport_preferences=None,
            must_clarify_before_search=True,
            must_not_invent_inventory=True,
            must_reject_unsupported_constraints=True,
            unsupported_constraint_categories=tuple(dict.fromkeys(unsupported)),
        ),
        label_basis=(
            "PreferTripPlan structured origin, destination, date range, city count, hard "
            "constraints, and preference predicates provide the gold labels."
        ),
        conversion_notes=(
            "The complete 225-row curated test split is included without query filtering.",
            "Arrival and return time windows remain missing because dates do not imply "
            "corporate deadline windows.",
            "Unsupported itinerary, multi-city, local, and preference requirements must be "
            "surfaced rather than silently discarded.",
        ),
    )


def _open_transport_preferences(
    query: str,
    *,
    score: bool,
) -> tuple[str, ...] | None:
    if not score:
        return None
    train = any(keyword in query for keyword in ("高铁", "火车"))
    flight = any(keyword in query for keyword in ("飞机", "航班"))
    if train and flight:
        return ("compare_train_and_flight",)
    if train and not any(keyword in query for keyword in ("自驾", "开车", "油费")):
        return ("prefer_train",)
    if flight:
        return ("prefer_flight",)
    return None


def _find_open_file(root: Path, name: str) -> Path:
    candidates = (
        root / name,
        root / "test" / name,
        root / "train" / name,
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"Open-Travel source file is unavailable: {name}")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {path}:{line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"Expected object at {path}:{line_number}")
            rows.append(value)
    return rows


def _require_hash(path: Path, expected: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError(f"Source hash mismatch for {path.name}: {actual}")


def _jsonl_bytes(models: list[object]) -> bytes:
    return b"".join(
        canonical_json_bytes(model.model_dump(mode="json")) + b"\n"
        for model in models
    )


def _write_new(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


if __name__ == "__main__":
    main()
