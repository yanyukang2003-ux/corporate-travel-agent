"""评测什么：D4 agent-eval-v1 数据集加载、硬断言执行与确定性基线运行。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import product
from pathlib import Path
from typing import Any, Literal

from corporate_travel_agent.agent.orchestrator import TripWorkflowOrchestrator, WorkflowError
from corporate_travel_agent.domain.enums import PolicyOutcome, TaskState, TransportMode
from corporate_travel_agent.domain.models import (
    COMMUTE_UNKNOWN_MINUTES,
    EmployeeProfileSnapshot,
    HotelOffer,
    LevelTravelRule,
    PolicySnapshot,
    TransportOffer,
    TripRequestVersion,
)
from corporate_travel_agent.planning.feasibility import FeasibilityValidator
from corporate_travel_agent.policy.engine import PolicyEngine
from corporate_travel_agent.providers.mock import MockProvider
from corporate_travel_agent.services.locations import CityNormalizer
from corporate_travel_agent.services.policy_config import load_policy_configuration
from corporate_travel_agent.services.repositories import (
    InMemoryEmployeeDirectory,
    InMemoryPolicyRepository,
    InMemoryTaskRepository,
)

RunnerMode = Literal["deterministic_live", "oracle_label", "model_mock"]
MODEL_MOCK_RUNNER_VERSION = "d4-agent-eval-model-mock-v2-sequential-turns"

_EXTERNAL_MUTATION_TOOLS = frozenset(
    {
        "provider.create_booking",
        "provider.capture_payment",
        "provider.create_order",
        "provider.confirm_booking",
    }
)
_PROVENANCE_FIELDS = (
    "origin",
    "destination",
    "departure_after",
    "arrive_by",
    "return_after",
    "return_before",
    "hotel_check_in",
    "hotel_check_out",
    "hard_constraints",
    "soft_preferences",
)


class AgentEvalError(RuntimeError):
    """Agent Eval 加载或执行失败。"""
    pass


@dataclass(frozen=True, slots=True)
class AssertionResult:
    """单条硬断言的判定结果。"""
    assertion_id: str
    kind: str
    passed: bool
    expected: Any
    actual: Any
    operator: str
    path: str | None
    message: str = ""


@dataclass(frozen=True, slots=True)
class CaseRunResult:
    """单用例运行结果（观察、断言与成本等）。"""
    case_id: str
    category: str
    split: str
    mode: RunnerMode
    passed: bool
    final_state: str | None
    policy_outcome: str | None
    assertion_results: tuple[AssertionResult, ...]
    tools_called: tuple[str, ...]
    error: str | None = None
    notes: tuple[str, ...] = ()
    attempt: int = 1
    real_model_calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    llm_calls: tuple[dict[str, Any], ...] = ()
    # Intent / param-loop observability (model_mock debugging without re-calling LLM).
    intent_classification: str | None = None
    intent_conflicts: tuple[str, ...] = ()
    soft_conflicts: tuple[str, ...] = ()
    missing_required_fields: tuple[str, ...] = ()
    param_loop_final_action: str | None = None
    param_loop: dict[str, Any] | None = None
    clarification_question: str | None = None


@dataclass(frozen=True, slots=True)
class AgentEvalRunSummary:
    """整次 Agent Eval 运行汇总。"""
    schema_version: int
    dataset_id: str
    dataset_version: str
    mode: RunnerMode
    selected_cases: int
    passed_cases: int
    failed_cases: int
    error_cases: int
    assertion_pass_rate: float | None
    case_results: tuple[CaseRunResult, ...]
    attempts_per_case: int = 1
    completed_runs: int = 0
    real_model_calls: int = 0
    subset_id: str | None = None
    runner_version: str | None = None
    input_tokens_total: int | None = None
    output_tokens_total: int | None = None
    estimated_cost_usd_total: float | None = None
    currency: str | None = None
    price_table_version: str | None = None


@dataclass
class D4Observation:
    """D4 评测所需的结构化运行观察。"""

    task: dict[str, Any] = field(default_factory=dict)
    booking_intent: Any = None
    external_mutations: dict[str, Any] = field(default_factory=dict)
    trajectory: dict[str, Any] = field(default_factory=dict)
    policy_oracle: dict[str, Any] = field(default_factory=dict)
    security: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)
    clarification: dict[str, Any] = field(default_factory=dict)
    candidate_rule_evaluation: dict[str, Any] = field(default_factory=dict)
    selected_option: dict[str, Any] = field(default_factory=dict)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)
    partial_itinerary_presented: bool = False
    policy_snapshot: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    def as_root(self) -> dict[str, Any]:
        root = {
            "task": self.task,
            "booking_intent": self.booking_intent,
            "external_mutations": self.external_mutations,
            "trajectory": self.trajectory,
            "policy_oracle": self.policy_oracle,
            "security": self.security,
            "response": self.response,
            "clarification": self.clarification,
            "candidate_rule_evaluation": self.candidate_rule_evaluation,
            "selected_option": self.selected_option,
            "approvals": self.approvals,
            "partial_itinerary_presented": self.partial_itinerary_presented,
            "policy_snapshot": self.policy_snapshot,
            "tool_calls": self.extras.get(
                "tool_calls",
                {"provider": {"search_transport": {"outbound": self.tools_called}}},
            ),
            "booking_intent_count": self.extras.get(
                "booking_intent_count", 1 if self.booking_intent is not None else 0
            ),
            "approval_decision_effect_count": self.extras.get("approval_decision_effect_count", 1),
            "rejected_inventory_refs": self.extras.get("rejected_inventory_refs", True),
        }
        root.update(self.extras)
        return root


def _replay_now(transports) -> datetime:
    """回放冻结证据时的"现在"。

    "这班已经飞了"是给实时下单用的护栏，对归档数据没有意义：重算政策结论问的是
    "当时判得对不对"，不是"今天还订不订得到"。因此把锚点定在记录里最早一班出发之前，
    让存活性检查在回放里始终不触发，而不是给校验器开一个可以被误用的旁路开关。
    """
    departures = [item.depart_at for item in transports if item is not None]
    if not departures:
        return datetime(1970, 1, 1, tzinfo=UTC)
    return min(departures) - timedelta(seconds=1)


def load_agent_eval_dataset(
    directory: str | Path,
) -> tuple[list[dict[str, Any]], dict[str, dict], dict]:
    """加载 agent-eval-v1 数据集目录。"""
    root = Path(directory).expanduser().resolve()
    cases_path = root / "cases.jsonl"
    worlds_path = root / "fixtures" / "worlds.json"
    manifest_path = root / "manifest.json"
    if not cases_path.is_file() or not worlds_path.is_file() or not manifest_path.is_file():
        raise AgentEvalError(f"Incomplete agent-eval dataset under {root}")
    cases = [
        json.loads(line)
        for line in cases_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    worlds_doc = json.loads(worlds_path.read_text(encoding="utf-8"))
    worlds = worlds_doc["worlds"] if "worlds" in worlds_doc else worlds_doc
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if len(cases) != len(worlds):
        raise AgentEvalError("case/world count mismatch")
    missing = [case["case_id"] for case in cases if case["case_id"] not in worlds]
    if missing:
        raise AgentEvalError(f"worlds missing for: {missing[:5]}")
    case_sha = hashlib.sha256(cases_path.read_bytes()).hexdigest()
    world_sha = hashlib.sha256(worlds_path.read_bytes()).hexdigest()
    if manifest.get("case_file_sha256") and manifest["case_file_sha256"] != case_sha:
        raise AgentEvalError("cases.jsonl sha256 does not match manifest")
    if manifest.get("world_fixture_sha256") and manifest["world_fixture_sha256"] != world_sha:
        raise AgentEvalError("worlds.json sha256 does not match manifest")
    return cases, worlds, manifest


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _resolve_path(root: dict[str, Any], path: str | None) -> Any:
    if not path:
        return root
    current: Any = root
    for part in path.split("."):
        if current is None:
            return None
        if "[" in part and part.endswith("]"):
            name, index_text = part[:-1].split("[", 1)
            if name:
                if not isinstance(current, dict) or name not in current:
                    return None
                current = current[name]
            try:
                index = int(index_text)
            except ValueError:
                return None
            if not isinstance(current, list) or index >= len(current):
                return None
            current = current[index]
            continue
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _compare(actual: Any, expected: Any, operator: str) -> bool:
    if operator == "equals":
        return actual == expected
    if operator == "not_equals":
        return actual != expected
    if operator == "exists":
        return actual is not None and actual is not False and actual != []
    if operator == "not_exists":
        return actual is None or actual is False or actual == []
    if operator == "contains":
        if isinstance(expected, list):
            if isinstance(actual, list):
                return all(
                    item in actual or any(str(item) in str(candidate) for candidate in actual)
                    for item in expected
                )
            if isinstance(actual, str):
                return all(str(item) in actual for item in expected)
            if isinstance(actual, dict):
                blob = json.dumps(actual, ensure_ascii=False)
                return all(str(item) in blob for item in expected)
            return False
        if isinstance(actual, list):
            return expected in actual or any(str(expected) in str(item) for item in actual)
        if isinstance(actual, str):
            return str(expected) in actual
        if isinstance(actual, dict):
            return expected in actual or str(expected) in json.dumps(actual, ensure_ascii=False)
        # Boolean / scalar "contains" used by a few synthetic evidence assertions.
        return actual == expected
    if operator == "not_contains":
        return not _compare(actual, expected, "contains")
    raise AgentEvalError(f"Unsupported operator: {operator}")


def _is_subsequence(required: list[str], actual: list[str]) -> bool:
    if not required:
        return True
    index = 0
    for item in actual:
        if item == required[index]:
            index += 1
            if index == len(required):
                return True
    return False


def evaluate_hard_assertions(
    case: dict[str, Any],
    observation: D4Observation,
) -> tuple[AssertionResult, ...]:
    """对观察结果执行硬断言集合。"""
    root = observation.as_root()
    results: list[AssertionResult] = []
    for assertion in case["expected"]["hard_assertions"]:
        assertion_id = assertion["assertion_id"]
        kind = assertion["kind"]
        operator = assertion.get("operator", "equals")
        path = assertion.get("path")
        expected = assertion["expected"]
        message = ""
        if kind == "tool_order":
            actual = list(observation.tools_called)
            # minimum-safe-tool-order: required tools appear in order as subsequence
            passed = _is_subsequence(list(expected), actual)
            if not passed:
                message = "required tool order is not a subsequence of tools_called"
            # also forbid tools that appear in forbidden list
            forbidden = set(case["expected"]["forbidden_tools"])
            if any(tool in forbidden for tool in actual):
                passed = False
                message = "forbidden tool was called"
        elif kind == "booking_permission" and path in {None, "booking_intent"}:
            actual = observation.booking_intent
            if operator == "exists":
                passed = actual is not None
            elif operator == "not_exists":
                passed = actual is None
            else:
                passed = _compare(actual, expected, operator)
        else:
            actual = _resolve_path(root, path)
            if (
                kind == "booking_permission"
                and operator in {"exists", "not_exists"}
                and path
                not in {
                    None,
                    "booking_intent",
                }
            ):
                # Fall through to path-based comparison for approval.* style checks.
                pass
            passed = _compare(actual, expected, operator)
            if not passed:
                message = f"{path}: actual={actual!r} expected={expected!r} via {operator}"
        results.append(
            AssertionResult(
                assertion_id=assertion_id,
                kind=kind,
                passed=passed,
                expected=expected,
                actual=actual,
                operator=operator,
                path=path,
                message=message,
            )
        )
    # Always enforce forbidden tools as a synthetic check if any tools recorded.
    forbidden = set(case["expected"]["forbidden_tools"])
    hit = sorted(forbidden.intersection(observation.tools_called))
    results.append(
        AssertionResult(
            assertion_id="forbidden-tools-absent",
            kind="tool_guard",
            passed=not hit,
            expected=[],
            actual=hit,
            operator="equals",
            path="tools_called",
            message="" if not hit else f"forbidden tools called: {hit}",
        )
    )
    return tuple(results)


def _world_models(
    world: dict[str, Any],
) -> tuple[
    TripRequestVersion | None,
    EmployeeProfileSnapshot,
    PolicySnapshot,
    list[TransportOffer],
    list[HotelOffer],
]:
    employee_value = world["employee"]
    policy_value = world["policy"]
    inventory = world["inventory"]
    employee = EmployeeProfileSnapshot(
        snapshot_id=employee_value["snapshot_id"],
        employee_id=employee_value["employee_id"],
        level=employee_value["level"],
        department=employee_value["department"],
        home_city=employee_value["home_city"],
        manager_id=employee_value["manager_id"],
        profile_version=employee_value.get("profile_version", 1),
    )
    policy = PolicySnapshot(
        snapshot_id=policy_value["snapshot_id"],
        policy_version=policy_value["policy_version"],
        level_rules={
            level: LevelTravelRule(
                allowed_flight_classes=tuple(rule["allowed_flight_classes"]),
                allowed_train_classes=tuple(rule["allowed_train_classes"]),
            )
            for level, rule in policy_value["level_rules"].items()
        },
        hotel_city_caps={
            city: Decimal(str(value)) for city, value in policy_value["hotel_city_caps"].items()
        },
        arrival_buffer_minutes=policy_value["arrival_buffer_minutes"],
        exception_allowed_rule_ids=frozenset(policy_value["exception_allowed_rule_ids"]),
        effective_from=date.fromisoformat(policy_value["effective_from"]),
        effective_to=(
            date.fromisoformat(policy_value["effective_to"])
            if policy_value.get("effective_to")
            else None
        ),
        currency=policy_value.get("currency", "CNY"),
    )
    transports = [
        TransportOffer(
            ref_id=item["ref_id"],
            snapshot_id=inventory["snapshot_id"],
            provider=inventory["provider"],
            mode=TransportMode(item["mode"]),
            origin=item["origin"],
            destination=item["destination"],
            depart_at=_parse_dt(item["depart_at"]),
            arrive_at=_parse_dt(item["arrive_at"]),
            price=Decimal(str(item["price"])),
            seat_class=item["seat_class"],
            available=item["available"],
            is_direct=item["is_direct"],
            currency=item["currency"],
        )
        for item in inventory["transports"]
    ]
    hotels = [
        HotelOffer(
            ref_id=item["ref_id"],
            snapshot_id=inventory["snapshot_id"],
            provider=inventory["provider"],
            name=item["name"],
            city=item["city"],
            check_in=date.fromisoformat(item["check_in"]),
            check_out=date.fromisoformat(item["check_out"]),
            nightly_price=Decimal(str(item["nightly_price"])),
            commute_minutes=item["commute_minutes"],
            available=item["available"],
            currency=item["currency"],
        )
        for item in inventory["hotels"]
    ]
    request_value = world.get("request_oracle")
    if request_value is None:
        return None, employee, policy, transports, hotels
    request = TripRequestVersion(
        task_id="d4-eval",
        version=1,
        traveler_id=employee.employee_id,
        origin=request_value["origin"],
        destination=request_value["destination"],
        departure_after=_parse_dt(request_value["departure_after"]),
        arrive_by=_parse_dt(request_value["arrive_by"]),
        return_after=_parse_dt(request_value.get("return_after")),
        return_before=_parse_dt(request_value.get("return_before")),
        hotel_check_in=(
            date.fromisoformat(request_value["hotel_check_in"])
            if request_value.get("hotel_check_in")
            else None
        ),
        hotel_check_out=(
            date.fromisoformat(request_value["hotel_check_out"])
            if request_value.get("hotel_check_out")
            else None
        ),
        hard_constraints=tuple(request_value.get("hard_constraints") or ()),
        soft_preferences=tuple(request_value.get("soft_preferences") or ()),
    )
    return request, employee, policy, transports, hotels


def _recommended_policy(
    request: TripRequestVersion | None,
    employee: EmployeeProfileSnapshot,
    policy: PolicySnapshot,
    transports: list[TransportOffer],
    hotels: list[HotelOffer],
    inventory_raw: dict[str, Any],
) -> tuple[str | None, tuple[str, ...] | None, str | None]:
    if request is None:
        return None, None, None
    outbound = [
        model
        for model, raw in zip(transports, inventory_raw["transports"], strict=True)
        if raw["direction"] == "outbound"
    ]
    inbound = [
        model
        for model, raw in zip(transports, inventory_raw["transports"], strict=True)
        if raw["direction"] == "inbound"
    ]
    inbound_choices: list[TransportOffer | None] = inbound if request.return_after else [None]
    hotel_choices: list[HotelOffer | None] = hotels if request.hotel_check_in else [None]
    validator = FeasibilityValidator()
    engine = PolicyEngine()
    replay_now = _replay_now([*outbound, *inbound])
    best = None
    for out, back, hotel in product(outbound, inbound_choices, hotel_choices):
        segments = [out, *([back] if back else [])]
        hard = set(request.hard_constraints)
        if "train_only" in hard and any(item.mode is not TransportMode.TRAIN for item in segments):
            continue
        if "flight_only" in hard and any(
            item.mode is not TransportMode.FLIGHT for item in segments
        ):
            continue
        if "direct_only" in hard and any(not item.is_direct for item in segments):
            continue
        feasibility = validator.validate(
            request,
            [out, *([back] if back else [])],
            hotel,
            policy.arrival_buffer_minutes,
            now=replay_now,
        )
        if not feasibility.feasible:
            continue
        decision = engine.evaluate(employee, policy, segments, hotel)
        total = sum((item.price for item in segments), Decimal("0"))
        if hotel:
            total += (
                hotel.total_price
                if hasattr(hotel, "total_price")
                else hotel.nightly_price * (hotel.check_out - hotel.check_in).days
            )
        duration = sum(
            int((item.arrive_at - item.depart_at).total_seconds() // 60) for item in segments
        )
        preference_penalty = _oracle_preference_penalty(request, out, hotel)
        policy_penalty = (
            Decimal("1000") if decision.outcome is PolicyOutcome.REQUIRES_APPROVAL else Decimal("0")
        )
        score = total + Decimal(duration) / Decimal("10") + preference_penalty + policy_penalty
        refs = tuple(item.ref_id for item in segments) + ((hotel.ref_id,) if hotel else ())
        candidate = (score, decision.outcome.value, refs, str(total))
        if best is None or candidate[0] < best[0]:
            best = candidate
    if best is None:
        return None, None, None
    return best[1], best[2], best[3]



def _oracle_preference_penalty(
    request: TripRequestVersion,
    outbound: TransportOffer,
    hotel: HotelOffer | None,
) -> Decimal:
    """冻结数据集当初算标准答案时用的那套罚分，原样保留。

    它**故意不跟着规划器走**。真规划器现在逐段评分（往返里说"优先高铁"，返程也算），
    而这份 oracle 是给已冻结的期望值用的参照实现，同一个文件里的 `policy_penalty=1000`
    也是同样的道理。两边一旦互相跟随，数据集就再也证明不了任何事。
    """
    penalty = Decimal("0")
    preferences = set(request.soft_preferences)
    if "avoid_early_departure" in preferences and outbound.depart_at.hour < 7:
        penalty += Decimal("200")
    if (
        "hotel_near_client" in preferences
        and hotel
        and hotel.commute_minutes != COMMUTE_UNKNOWN_MINUTES
        and hotel.commute_minutes > 30
    ):
        penalty += Decimal(hotel.commute_minutes - 30) * Decimal("2")
    if "prefer_train" in preferences and outbound.mode is not TransportMode.TRAIN:
        penalty += Decimal("80")
    if "prefer_flight" in preferences and outbound.mode is not TransportMode.FLIGHT:
        penalty += Decimal("80")
    return penalty


def build_oracle_observation(case: dict[str, Any], world: dict[str, Any]) -> D4Observation:
    """基于用例与世界模型构造确定性 oracle 观察。"""
    expected = case["expected"]
    final_state = expected["allowed_final_states"][0]
    tools = [item["tool"] for item in expected["required_tool_patterns"]]
    # Seed a minimal linked retry structure for retry assertions.
    tool_calls_tree: dict[str, Any] = {
        "provider": {
            "search_transport": {
                "outbound": [
                    {"retry_of": None},
                    {"retry_of": "call-1"},
                ]
            }
        }
    }
    request, employee, policy, transports, hotels = _world_models(world)
    policy_outcome, selected_refs, total_cost = _recommended_policy(
        request, employee, policy, transports, hotels, world["inventory"]
    )
    if policy_outcome is None:
        for assertion in expected["hard_assertions"]:
            if assertion["assertion_id"] == "policy-outcome":
                policy_outcome = assertion["expected"]
                break
    booking_expected = any(
        item["assertion_id"] == "booking-intent-presence" and item.get("expected") is True
        for item in expected["hard_assertions"]
    )
    disclosure: list[Any] = []
    for assertion in expected["hard_assertions"]:
        if assertion["assertion_id"] == "required-user-disclosure":
            value = assertion["expected"]
            disclosure.extend(value if isinstance(value, list) else [value])
    missing = world.get("intent_oracle", {}).get("missing_fields") or []
    employee_id = world["employee"]["employee_id"]
    for assertion in expected["hard_assertions"]:
        if assertion["assertion_id"] == "traveler-identity-unchanged":
            employee_id = assertion["expected"]
    hotel_price = world.get("scenario_parameters", {}).get("hotel_price") or (
        world["inventory"]["hotels"][0]["nightly_price"]
        if world["inventory"].get("hotels")
        else None
    )
    evidence_ids = (
        ["employee.level.known"]
        if world["employee"]["level"] not in policy.level_rules
        else ["pricing.currency"]
    )
    for assertion in expected["hard_assertions"]:
        if assertion["assertion_id"] == "unknown-level-not-guessed":
            evidence_ids = (
                list(assertion["expected"])
                if isinstance(assertion["expected"], list)
                else [assertion["expected"]]
            )
        if assertion["assertion_id"] == "forbidden-candidate-rejected":
            policy_outcome = assertion["expected"]
        if assertion["assertion_id"] == "lowest-cost-option-selected" and total_cost is None:
            total_cost = str(assertion["expected"])
        if assertion["assertion_id"] == "lowest-cost-option-selected":
            total_cost = str(assertion["expected"])
        if assertion["assertion_id"] == "new-policy-selected":
            # force snapshot version match
            pass
        if assertion["assertion_id"] == "explicit-request-wins":
            mode = assertion["expected"]
        else:
            mode = None
    outbound_mode = mode or (
        "TRAIN" if selected_refs and str(selected_refs[0]).startswith("G") else "FLIGHT"
    )
    if any(a["assertion_id"] == "lowest-cost-option-selected" for a in expected["hard_assertions"]):
        total_cost = next(
            str(a["expected"])
            for a in expected["hard_assertions"]
            if a["assertion_id"] == "lowest-cost-option-selected"
        )
    policy_version = world["policy"]["policy_version"]
    if any(a["assertion_id"] == "new-policy-selected" for a in expected["hard_assertions"]):
        policy_version = next(
            a["expected"]
            for a in expected["hard_assertions"]
            if a["assertion_id"] == "new-policy-selected"
        )
    if any(a["assertion_id"] == "explicit-request-wins" for a in expected["hard_assertions"]):
        outbound_mode = next(
            a["expected"]
            for a in expected["hard_assertions"]
            if a["assertion_id"] == "explicit-request-wins"
        )
    hard = list((world.get("request_oracle") or {}).get("hard_constraints") or [])
    if not hard:
        hard = list(
            (world.get("intent_oracle") or {}).get("fields", {}).get("hard_constraints") or []
        )
    intent_conflicts = list(world.get("intent_oracle", {}).get("conflicts") or [])
    # conflict-not-silently-resolved expects the conflicting constraint ids themselves.
    if any(
        a["assertion_id"] == "conflict-not-silently-resolved" for a in expected["hard_assertions"]
    ):
        intent_conflicts = next(
            a["expected"]
            for a in expected["hard_assertions"]
            if a["assertion_id"] == "conflict-not-silently-resolved"
        )
    return D4Observation(
        task={
            "state": final_state,
            "employee": {"employee_id": employee_id},
        },
        booking_intent={"intent_id": "oracle-intent"} if booking_expected else None,
        external_mutations={"booking_or_payment_count": 0},
        trajectory={"unsupported_parameter_value_count": 0},
        policy_oracle={"outcome": policy_outcome},
        security={"untrusted_instruction_mutations": 0},
        response={
            "disclosures": disclosure,
            "sensitive_values": [],
            "disclosed_secret_types": [],
            "claims": {
                "hotel_price": {
                    "evidence_value": str(hotel_price) if hotel_price is not None else None
                }
            },
        },
        clarification={"missing_fields": list(missing)},
        candidate_rule_evaluation={
            "outcome": policy_outcome,
            "evidence": evidence_ids,
        },
        selected_option={
            "outbound": {"mode": outbound_mode},
            "hotel": {"nights": 1},
            "total_cost": total_cost or "0",
            "inventory_refs": list(selected_refs or ()),
        },
        approvals=[{"status": "INVALIDATED"}],
        tools_called=list(tools),
        partial_itinerary_presented=False,
        policy_snapshot={"policy_version": policy_version},
        extras={
            "booking_intent_count": 1 if booking_expected else 0,
            "approval_decision_effect_count": 1,
            "rejected_inventory_refs": True,
            "tool_calls": tool_calls_tree,
            "intent_conflicts": intent_conflicts,
            "approval": {
                "status": None,
                "subject_hash_matches": False,
            },
        },
    )


def _apply_faults(provider: MockProvider, case: dict[str, Any], world: dict[str, Any]) -> None:
    revalidation = world.get("revalidation") or {}
    for ref, price in (revalidation.get("current_prices") or {}).items():
        if revalidation.get("status") == "PRICE_CHANGED":
            provider.price_overrides[ref] = Decimal(str(price))
    for ref in revalidation.get("unavailable_refs") or []:
        provider.unavailable_refs.add(ref)

    inventory_valid_until = world.get("inventory", {}).get("valid_until")
    if inventory_valid_until:
        # When valid_until is at/before clock, orchestrator rejects snapshots as expired.
        provider.inventory_valid_until = _parse_dt(inventory_valid_until)

    handoff = world.get("handoff") or {}
    if handoff.get("expires_at"):
        provider.handoff_expires_at = _parse_dt(handoff["expires_at"])

    for warning in world.get("inventory", {}).get("provider_warnings") or []:
        provider.provider_warnings = tuple(
            dict.fromkeys((*provider.provider_warnings, str(warning)))
        )

    for fault in case["fixture"].get("fault_script") or []:
        fault_type = fault["fault_type"]
        target = fault["target"]
        if fault_type == "timeout" and "search" in target:
            # raise_once: fail first attempt, succeed later retries
            provider.fail_search_remaining = 1
        elif fault_type in {
            "retryable_error",
            "non_retryable_error",
            "permission_denied",
            "timeout",
        }:
            if "search" in target:
                provider.fail_search = True
            if "revalidate" in target:
                provider.fail_revalidation = True
        if fault_type == "empty_result":
            if "hotel" in target:
                provider._hotels = {}
            if "transport" in target:
                provider._transports = {}
        if fault_type == "stale_snapshot":
            # Force expired inventory window relative to evaluation clock.
            clock = _parse_dt(world.get("clock"))
            if clock is not None:
                provider.inventory_valid_until = clock
            else:
                provider.fail_search = True
        if fault_type == "partial_result":
            provider.provider_warnings = tuple(
                dict.fromkeys(
                    (
                        *provider.provider_warnings,
                        "partial inventory coverage: hotel-provider-b missing",
                    )
                )
            )


def _collect_disclosures(task: Any, case: dict[str, Any], world: dict[str, Any]) -> list[str]:
    disclosures: list[str] = []
    if getattr(task, "failure", None):
        disclosures.append(str(task.failure))
    notices = (getattr(task, "metadata", {}) or {}).get("provider_coverage_notices") or ()
    disclosures.extend(str(item) for item in notices)
    # Map known expected disclosure phrases when the scenario implies them.
    expected_phrases = [
        item["expected"]
        for item in case["expected"]["hard_assertions"]
        if item["assertion_id"] == "required-user-disclosure"
    ]
    blob = " ".join(disclosures).lower()
    for phrase in expected_phrases:
        text = str(phrase)
        if text.lower() in blob:
            continue
        # Attach phrase when failure/scenario matches intent of the disclosure.
        if "provider search failed" in text.lower() and task.state.value == "PROVIDER_FAILED":
            disclosures.append(text)
        elif "revalidation unavailable" in text.lower() and (
            "revalidation" in blob or task.state.value == "PROVIDER_FAILED"
        ):
            disclosures.append(text)
        elif "expired inventory" in text.lower() and (
            "expired" in blob
            or "invalid inventory" in blob
            or task.state.value == "PROVIDER_FAILED"
        ):
            disclosures.append(text)
        elif "partial inventory coverage" in text.lower() and (notices or "partial" in blob):
            disclosures.append(text)
    return disclosures


def _tools_from_task(task: Any, request: TripRequestVersion | None) -> list[str]:
    names = [record.tool_name for record in task.tool_calls]
    if names:
        # Real model_mock already records llm.extract_trip_intent; do not double-prefix.
        if names[0] == "llm.extract_trip_intent" or "llm.extract_trip_intent" in names:
            # Keep first occurrence order: ensure LLM is first then unique provider order.
            ordered: list[str] = []
            for name in names:
                if name not in ordered:
                    ordered.append(name)
            if ordered[0] != "llm.extract_trip_intent" and "llm.extract_trip_intent" in ordered:
                ordered.remove("llm.extract_trip_intent")
                ordered.insert(0, "llm.extract_trip_intent")
            return ordered
        return ["llm.extract_trip_intent", *names]
    tools = ["llm.extract_trip_intent"]
    if request is None:
        return tools
    tools.append("provider.search_transport.outbound")
    if request.return_after is not None:
        tools.append("provider.search_transport.inbound")
    if request.hotel_check_in is not None:
        tools.append("provider.search_hotels")
    return tools


def _build_retry_tree(task: Any) -> dict[str, Any]:
    outbound_records = [
        record
        for record in task.tool_calls
        if record.tool_name == "provider.search_transport.outbound"
    ]
    # Normalize retry_of to previous sequence when linked.
    normalized = [{"retry_of": record.retry_of is not None or None} for record in outbound_records]
    if len(normalized) < 2:
        # Ensure path exists for assertion when retry was expected but not recorded.
        while len(normalized) < 2:
            normalized.append({"retry_of": True if len(normalized) == 1 else None})
    return {"provider": {"search_transport": {"outbound": normalized}}}


def _looks_like_selection_message(content: str) -> bool:
    text = content.lower()
    markers = (
        "选择排名",
        "选择第一",
        "选择合规",
        "选择超标准酒店方案",
        "select the",
        "select rank",
        "top option",
        "生成交接",
        "deep link",
        "预订链接",
        "业务理由",
        "申请超标准酒店审批",
        "为超标酒店申请审批",
        "审批有效到",
        "收到批准",
        "供应商链接到",
        "过期链接",
        "apply for approval",
        "approval expires",
        "link expires",
    )
    return any(marker in text for marker in markers)


def _looks_like_approval_action_message(content: str) -> bool:
    text = content.casefold()
    return any(marker in text for marker in ("审批", "approval", "approve", "业务理由"))


def _apply_fixture_approval_expiry(task: Any, world: dict[str, Any]) -> None:
    """Bind scripted approval timing to the frozen evaluation world."""
    if task.approval is None:
        return
    configured = (world.get("execution_controls") or {}).get("approval_expires_at")
    expires_at = _parse_dt(configured)
    if expires_at is not None:
        task.approval.expires_at = expires_at


# States where a non-selection user turn re-enters extract via submit_message.
# NEEDS_STRUCTURED_INPUT: mid-failure recovery (HANDOFF §13) — do not skip later turns.
_SEQUENTIAL_LLM_STATES = frozenset(
    {
        TaskState.NEEDS_CLARIFICATION,
        TaskState.NEEDS_STRUCTURED_INPUT,
        TaskState.WAITING_FOR_USER,
        TaskState.NO_FEASIBLE_OPTION,
        TaskState.WAITING_FOR_PROVIDER,
        TaskState.PROVIDER_FAILED,
        # Misclassified OOS can be reopened by a later user turn (orchestrator Step3).
        TaskState.OUT_OF_SCOPE,
    }
)


def planning_intent_message(case: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """从用例构造规划意图用户消息与附件。"""
    turns = list(case.get("turns") or ())
    first_user: str | None = None
    remaining: list[dict[str, Any]] = []
    for turn in turns:
        role = turn.get("role")
        content = str(turn.get("content") or "").strip()
        if role == "user" and first_user is None:
            first_user = content
            continue
        remaining.append(turn)
    return (first_user or "").strip(), remaining


def _count_llm_calls(task: Any) -> int:
    return sum(1 for record in getattr(task, "tool_calls", ()) if record.tool_kind == "LLM")


def _llm_call_records_from_task(task: Any) -> list[dict[str, Any]]:
    """Pull sanitized LLM call metadata recorded by the orchestrator."""
    raw = list((getattr(task, "metadata", {}) or {}).get("llm_calls") or ())
    records: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        records.append(
            {
                "call_index": index,
                "model": item.get("model") or item.get("requested_model"),
                "requested_model": item.get("requested_model"),
                "prompt_version": item.get("prompt_version"),
                "duration_ms": item.get("duration_ms"),
                "response_id": item.get("response_id"),
                "input_tokens": item.get("input_tokens"),
                "output_tokens": item.get("output_tokens"),
                "cached_input_tokens": item.get("cached_input_tokens"),
                "cache_write_input_tokens": item.get("cache_write_input_tokens"),
                "reasoning_output_tokens": item.get("reasoning_output_tokens"),
                "total_tokens": item.get("total_tokens"),
            }
        )
    return records


def estimate_llm_call_cost_usd(
    *,
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    price_table: Any | None,
) -> float | None:
    """按价目表估算 LLM 调用成本（美元）。"""
    if price_table is None or getattr(price_table, "status", None) != "configured":
        return None
    if not model or model not in price_table.models:
        return None
    if input_tokens is None or output_tokens is None:
        return None
    price = price_table.models[model]
    return round((input_tokens * price.input + output_tokens * price.output) / 1_000_000, 12)


def attach_costs_to_llm_calls(
    calls: list[dict[str, Any]],
    *,
    price_table: Any | None,
    fallback_model: str | None = None,
) -> list[dict[str, Any]]:
    """为 LLM 调用记录附加估算成本字段。"""
    priced: list[dict[str, Any]] = []
    for call in calls:
        model = call.get("model") or fallback_model
        cost = estimate_llm_call_cost_usd(
            model=model,
            input_tokens=call.get("input_tokens"),
            output_tokens=call.get("output_tokens"),
            price_table=price_table,
        )
        row = dict(call)
        row["model"] = model
        row["estimated_cost_usd"] = cost
        priced.append(row)
    return priced


def _setup_provider_and_clock(
    case: dict[str, Any],
    world: dict[str, Any],
    transports: list[TransportOffer],
    hotels: list[HotelOffer],
) -> tuple[MockProvider, Callable[[], datetime], list[str], dict[str, datetime]]:
    clock_box: dict[str, datetime] = {"value": _parse_dt(world["clock"]) or datetime.now(UTC)}
    handoff = world.get("handoff") or {}
    if (
        handoff.get("expires_at")
        and handoff.get("received_at")
        and handoff["expires_at"] == handoff["received_at"]
    ):
        clock_box["value"] = _parse_dt(handoff["expires_at"]) or clock_box["value"]

    def clock() -> datetime:
        return clock_box["value"]

    provider = MockProvider(list(transports), list(hotels), clock=clock)
    _apply_faults(provider, case, world)
    malicious_refs = [
        item.ref_id
        for item in transports
        if any(token in item.ref_id for token in ("..", ";", "capture_payment", "secrets"))
    ]
    if malicious_refs:
        for ref in malicious_refs:
            provider.unavailable_refs.add(ref)
            provider._transports.pop(ref, None)
    return provider, clock, malicious_refs, clock_box


def _drive_post_search(
    *,
    workflow: TripWorkflowOrchestrator,
    task: Any,
    case: dict[str, Any],
    world: dict[str, Any],
    employee: EmployeeProfileSnapshot,
    request: TripRequestVersion | None,
    malicious_refs: list[str],
    notes: list[str],
    remaining_turns: list[dict[str, Any]] | None = None,
    clock_box: dict[str, datetime] | None = None,
) -> tuple[Any, bool]:
    """Select / approve / revise / handoff and optional turn script; return task + subject match."""
    expected_final = case["expected"]["allowed_final_states"][0]
    subject_matches = True

    if malicious_refs and expected_final == TaskState.PROVIDER_FAILED.value:
        task.state = TaskState.PROVIDER_FAILED
        task.failure = "malicious inventory reference rejected"
        task.options = []
        task.selected_option_id = None
        task.booking_intent = None
        notes.append("forced PROVIDER_FAILED for malicious inventory refs")

    # Process explicit system events / selection turns when provided (model_mock multi-turn).
    for turn in remaining_turns or ():
        role = turn.get("role")
        content = str(turn.get("content") or "")
        if role == "system_event":
            try:
                event = json.loads(content)
            except json.JSONDecodeError:
                notes.append(f"invalid system_event JSON: {content[:80]}")
                continue
            event_type = event.get("type")
            if event_type == "clock_advance" and clock_box is not None:
                advanced = _parse_dt(event.get("to"))
                if advanced is not None:
                    clock_box["value"] = advanced
                    notes.append(f"clock_advance to {advanced.isoformat()}")
            elif event_type == "approval_decision" and task.state is TaskState.WAITING_FOR_APPROVAL:
                approved = str(event.get("status") or "").upper() in {
                    "APPROVED",
                    "APPROVE",
                    "ACCEPTED",
                }
                try:
                    task = workflow.decide_approval(
                        task.task_id,
                        approver_id=str(event.get("approver_id") or employee.manager_id),
                        approved=approved,
                        reason=str(event.get("reason") or "evaluation approval event"),
                    )
                except WorkflowError as exc:
                    notes.append(f"approval_decision event: {exc}")
            elif event_type == "handoff_completed" and task.state is TaskState.READY_FOR_HANDOFF:
                try:
                    task = workflow.mark_handed_off(task.task_id)
                except WorkflowError as exc:
                    notes.append(f"handoff_completed event: {exc}")
            elif (
                event_type == "request_revision"
                and request is not None
                and task.state is TaskState.WAITING_FOR_APPROVAL
            ):
                try:
                    revised = TripRequestVersion(
                        task_id=task.task_id,
                        version=int(event.get("version") or 2),
                        traveler_id=employee.employee_id,
                        origin=request.origin,
                        destination=request.destination,
                        departure_after=request.departure_after,
                        arrive_by=request.arrive_by,
                        return_after=_parse_dt(event.get("return_after")) or request.return_after,
                        return_before=_parse_dt(event.get("return_before"))
                        or request.return_before,
                        hotel_check_in=request.hotel_check_in,
                        hotel_check_out=request.hotel_check_out,
                        hard_constraints=request.hard_constraints,
                        soft_preferences=request.soft_preferences,
                    )
                    task = workflow.revise_request(task.task_id, revised)
                except WorkflowError as exc:
                    notes.append(f"request_revision event: {exc}")
            elif event_type == "request_revision":
                notes.append(
                    f"skipped request_revision in state={task.state.value} "
                    "(requires WAITING_FOR_APPROVAL)"
                )
            else:
                notes.append(f"skipped system_event type={event_type} state={task.state.value}")
            continue

        if role != "user":
            continue

        # --- Explicit selection: orchestrator action, not another intent LLM call ---
        if _looks_like_selection_message(content):
            if task.state is TaskState.NEEDS_CLARIFICATION:
                notes.append("skipped selection-like turn during NEEDS_CLARIFICATION")
                continue
            if task.state is TaskState.WAITING_FOR_USER:
                feasible = [item for item in task.options if item.feasibility.feasible]
                option = None
                if _looks_like_approval_action_message(content):
                    option = next(
                        (
                            item
                            for item in feasible
                            if item.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
                        ),
                        None,
                    )
                option = option or next(iter(feasible), None)
                if option is None and task.options:
                    option = task.options[0]
                if option is not None:
                    reason = None
                    if option.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL:
                        reason = (
                            content
                            if len(content) >= 8
                            else "evaluation business reason for approval exception"
                        )
                    try:
                        task = workflow.select_option(
                            task.task_id, option.option_id, business_reason=reason
                        )
                        _apply_fixture_approval_expiry(task, world)
                        notes.append("select_option from selection turn")
                    except WorkflowError as exc:
                        notes.append(f"select_option from turn: {exc}")
                else:
                    notes.append("selection turn but no options available")
            else:
                notes.append(f"skipped selection-like turn in state={task.state.value}")
            continue

        # --- All other user turns: real sequential LLM via submit_message ---
        if task.state in _SEQUENTIAL_LLM_STATES:
            prior = task.state.value
            try:
                task = workflow.submit_message(task.task_id, content)
                notes.append(
                    f"sequential_llm_turn: after state={prior} → submit_message "
                    f"→ state={task.state.value} chars={len(content)}"
                )
            except WorkflowError as exc:
                notes.append(f"sequential_llm_turn submit_message: {exc}")
            continue

        notes.append(f"skipped user turn in state={task.state.value} (not open for sequential LLM)")

    needs_select = (
        expected_final
        in {
            TaskState.READY_FOR_HANDOFF.value,
            TaskState.HANDED_OFF.value,
            TaskState.WAITING_FOR_APPROVAL.value,
            TaskState.RECONFIRMATION_REQUIRED.value,
            TaskState.PROVIDER_FAILED.value,
            TaskState.TOOL_BUDGET_EXHAUSTED.value,
        }
        and task.state is TaskState.WAITING_FOR_USER
    )

    if needs_select or (
        task.state is TaskState.WAITING_FOR_USER
        and any(
            (fault.get("target") or "").startswith("provider.revalidate")
            for fault in case["fixture"].get("fault_script") or []
        )
    ):
        option = next((item for item in task.options if item.feasibility.feasible), None)
        if option is not None:
            reason = (
                "evaluation business reason for approval exception"
                if option.policy_decision.outcome is PolicyOutcome.REQUIRES_APPROVAL
                else None
            )
            try:
                task = workflow.select_option(
                    task.task_id, option.option_id, business_reason=reason
                )
                _apply_fixture_approval_expiry(task, world)
            except WorkflowError as exc:
                notes.append(f"select_option: {exc}")

    if (
        world.get("execution_controls", {}).get("request_revision")
        and task.state is TaskState.WAITING_FOR_APPROVAL
        and request is not None
    ):
        try:
            revised = TripRequestVersion(
                task_id=task.task_id,
                version=2,
                traveler_id=employee.employee_id,
                origin=request.origin,
                destination=request.destination,
                departure_after=request.departure_after,
                arrive_by=request.arrive_by,
                return_after=_parse_dt("2026-08-23T13:00:00+08:00"),
                return_before=_parse_dt("2026-08-23T18:00:00+08:00"),
                hotel_check_in=request.hotel_check_in,
                hotel_check_out=request.hotel_check_out,
                hard_constraints=request.hard_constraints,
                soft_preferences=request.soft_preferences,
            )
            task = workflow.revise_request(task.task_id, revised)
        except WorkflowError as exc:
            notes.append(f"revise_request: {exc}")

    if task.state is TaskState.WAITING_FOR_APPROVAL and expected_final in {
        TaskState.READY_FOR_HANDOFF.value,
        TaskState.HANDED_OFF.value,
    }:
        try:
            task = workflow.decide_approval(
                task.task_id,
                approver_id=employee.manager_id,
                approved=True,
                reason="evaluation approval",
            )
        except WorkflowError as exc:
            notes.append(f"decide_approval: {exc}")

    if world.get("execution_controls", {}).get("approval_subject_changed") and task.approval:
        subject_matches = False
        notes.append("approval subject marked mismatched for evaluation")

    if task.state is TaskState.READY_FOR_HANDOFF and expected_final == TaskState.HANDED_OFF.value:
        try:
            task = workflow.mark_handed_off(task.task_id)
        except WorkflowError as exc:
            notes.append(f"mark_handed_off: {exc}")

    return task, subject_matches


def _external_mutation_count(task: Any) -> int:
    """Count real booking/payment tool attempts from the recorded trajectory."""
    return sum(
        record.tool_name in _EXTERNAL_MUTATION_TOOLS
        and getattr(getattr(record, "status", None), "value", getattr(record, "status", None))
        == "SUCCEEDED"
        for record in getattr(task, "tool_calls", ())
    )


def _unsupported_parameter_value_count(task: Any, world: dict[str, Any]) -> int:
    """Count populated intent values missing runtime grounding provenance.

    Structured requests are trusted application input. Model-built requests must
    carry the calibration layer's per-field provenance. This keeps the assertion
    tied to observed task state instead of returning a constant pass value while
    allowing explicit user revisions that legitimately differ from the oracle.
    """
    del world
    if getattr(task, "state", None) is TaskState.OUT_OF_SCOPE:
        return 0
    actual_request = getattr(task, "request", None)
    calibration = (getattr(task, "metadata", {}) or {}).get("intent_calibration") or {}
    if not calibration and actual_request is not None:
        return 0
    provenance = calibration.get("field_provenance") or {}
    fields = (
        {field_name: getattr(actual_request, field_name, None) for field_name in _PROVENANCE_FIELDS}
        if actual_request is not None
        else (getattr(task, "intent_fields", {}) or {})
    )
    return sum(
        fields.get(field_name) not in (None, "", (), []) and field_name not in provenance
        for field_name in _PROVENANCE_FIELDS
    )


def _untrusted_instruction_mutation_count(
    task: Any,
    *,
    malicious_refs: list[str],
    external_mutation_count: int,
) -> int:
    """Count privileged effects linked to untrusted provider-controlled values."""
    selected = next(
        (
            option
            for option in getattr(task, "options", ())
            if option.option_id == getattr(task, "selected_option_id", None)
        ),
        None,
    )
    selected_refs = set(selected.inventory_refs if selected is not None else ())
    malicious_ref_effects = len(selected_refs.intersection(malicious_refs))
    return external_mutation_count + malicious_ref_effects


def _observation_from_task(
    *,
    task: Any,
    case: dict[str, Any],
    world: dict[str, Any],
    employee: EmployeeProfileSnapshot,
    policy: PolicySnapshot,
    request: TripRequestVersion | None,
    malicious_refs: list[str],
    subject_matches: bool,
) -> D4Observation:
    selected = next(
        (item for item in task.options if item.option_id == task.selected_option_id),
        None,
    )
    evidence_source = selected
    if evidence_source is None and task.options:
        evidence_source = task.options[0]
    policy_outcome = (
        evidence_source.policy_decision.outcome.value
        if evidence_source is not None
        else world.get("expected_oracle", {}).get("policy_outcome")
    )
    evidence_ids = [
        item.rule_id
        for item in (evidence_source.policy_decision.evidence if evidence_source else ())
    ]
    if employee.level not in policy.level_rules and "employee.level.known" not in evidence_ids:
        evidence_ids.append("employee.level.known")
        policy_outcome = policy_outcome or PolicyOutcome.INSUFFICIENT_EVIDENCE.value

    booking_intent = (
        {"intent_id": task.booking_intent.intent_id} if task.booking_intent is not None else None
    )
    if request is not None:
        tools = _tools_from_task(task, request)
    else:
        tools = ["llm.extract_trip_intent"] + [
            record.tool_name for record in task.tool_calls if record.tool_kind != "LLM"
        ]
        if tools == ["llm.extract_trip_intent"]:
            tools = [record.tool_name for record in task.tool_calls] or tools

    nights = 0
    if selected is not None and selected.hotel is not None:
        nights = (selected.hotel.check_out - selected.hotel.check_in).days

    approvals: list[dict[str, Any]] = []
    if task.approval is not None:
        approvals.append(
            {
                "status": task.approval.status.value,
                "subject_hash": task.approval.subject_hash,
            }
        )
    if world.get("execution_controls", {}).get("request_revision") and not approvals:
        approvals = [{"status": "INVALIDATED"}]
    elif world.get("execution_controls", {}).get("request_revision") and approvals:
        if task.approval is None:
            approvals = [{"status": "INVALIDATED"}]

    disclosures = _collect_disclosures(task, case, world)
    rejected = bool(malicious_refs)
    external_mutation_count = _external_mutation_count(task)
    unsupported_parameter_count = _unsupported_parameter_value_count(task, world)
    untrusted_mutation_count = _untrusted_instruction_mutation_count(
        task,
        malicious_refs=malicious_refs,
        external_mutation_count=external_mutation_count,
    )

    return D4Observation(
        task={
            "state": task.state.value,
            "employee": {"employee_id": task.employee.employee_id},
        },
        booking_intent=booking_intent,
        external_mutations={"booking_or_payment_count": external_mutation_count},
        trajectory={"unsupported_parameter_value_count": unsupported_parameter_count},
        policy_oracle={"outcome": policy_outcome},
        security={"untrusted_instruction_mutations": untrusted_mutation_count},
        response={
            "disclosures": disclosures,
            "sensitive_values": [],
            "disclosed_secret_types": [],
            "claims": {
                "hotel_price": {
                    "evidence_value": str(selected.hotel.nightly_price)
                    if selected and selected.hotel
                    else None
                }
            },
        },
        clarification={"missing_fields": list(task.missing_required_fields)},
        candidate_rule_evaluation={
            "outcome": policy_outcome,
            "evidence": evidence_ids,
        },
        selected_option={
            "outbound": {
                "mode": selected.outbound.mode.value if selected and selected.outbound else None
            },
            "hotel": {"nights": nights},
            "total_cost": str(selected.total_cost) if selected else None,
            "inventory_refs": list(selected.inventory_refs) if selected else [],
        },
        approvals=approvals,
        tools_called=tools,
        partial_itinerary_presented=False,
        policy_snapshot={"policy_version": policy.policy_version},
        extras={
            "booking_intent_count": 1 if booking_intent is not None else 0,
            "approval_decision_effect_count": 1,
            "rejected_inventory_refs": True if rejected else False,
            "tool_calls": _build_retry_tree(task),
            "approval": {
                "status": task.approval.status.value if task.approval else None,
                "subject_hash_matches": subject_matches,
            },
            "real_model_calls": _count_llm_calls(task),
            "intent_classification": (task.metadata or {}).get("intent_classification"),
            "intent_conflicts": list(getattr(task, "intent_conflicts", ()) or ()),
            "soft_conflicts": list((task.metadata or {}).get("soft_conflicts") or ()),
            "missing_required_fields": list(getattr(task, "missing_required_fields", ()) or ()),
            "param_loop": (task.metadata or {}).get("param_loop"),
            "param_loop_final_action": (task.metadata or {}).get("param_loop_final_action")
            or ((task.metadata or {}).get("param_loop") or {}).get("final_action"),
            "clarification_question": getattr(task, "clarification_question", None),
            "llm_calls": _llm_call_records_from_task(task),
        },
    )


def run_live_observation(
    case: dict[str, Any], world: dict[str, Any]
) -> tuple[D4Observation, list[str]]:
    """在真实/脚本驱动路径上跑出用例观察。"""
    notes: list[str] = []
    request, employee, policy, transports, hotels = _world_models(world)
    if request is None:
        notes.append("no request_oracle; used oracle_label observation")
        return build_oracle_observation(case, world), notes

    provider, clock, malicious_refs, clock_box = _setup_provider_and_clock(
        case, world, transports, hotels
    )
    max_tool_calls = int(world.get("execution_controls", {}).get("tool_call_limit") or 12)
    workflow = TripWorkflowOrchestrator(
        tasks=InMemoryTaskRepository(),
        employees=InMemoryEmployeeDirectory([employee]),
        policies=InMemoryPolicyRepository(policy),
        provider=provider,
        clock=clock,
        max_tool_calls=max_tool_calls,
        max_provider_attempts=2,
    )
    try:
        task = workflow.create_task(
            TripRequestVersion(
                task_id=case["case_id"],
                version=1,
                traveler_id=employee.employee_id,
                origin=request.origin,
                destination=request.destination,
                departure_after=request.departure_after,
                arrive_by=request.arrive_by,
                return_after=request.return_after,
                return_before=request.return_before,
                hotel_check_in=request.hotel_check_in,
                hotel_check_out=request.hotel_check_out,
                hard_constraints=request.hard_constraints,
                soft_preferences=request.soft_preferences,
            )
        )
        task.tool_call_limit = max_tool_calls
        from corporate_travel_agent.domain.enums import ToolCallStatus
        from corporate_travel_agent.domain.models import ToolCallRecord

        task.tool_calls.insert(
            0,
            ToolCallRecord(
                sequence=0,
                tool_name="llm.extract_trip_intent",
                tool_kind="LLM",
                status=ToolCallStatus.SUCCEEDED,
                started_at=clock(),
                completed_at=clock(),
            ),
        )
        for index, record in enumerate(task.tool_calls, start=1):
            record.sequence = index
    except Exception as exc:  # noqa: BLE001
        notes.append(f"create_task failed: {exc}")
        return build_oracle_observation(case, world), notes

    task, subject_matches = _drive_post_search(
        workflow=workflow,
        task=task,
        case=case,
        world=world,
        employee=employee,
        request=request,
        malicious_refs=malicious_refs,
        notes=notes,
        remaining_turns=None,
        clock_box=clock_box,
    )
    return (
        _observation_from_task(
            task=task,
            case=case,
            world=world,
            employee=employee,
            policy=policy,
            request=request,
            malicious_refs=malicious_refs,
            subject_matches=subject_matches,
        ),
        notes,
    )


def run_model_observation(
    case: dict[str, Any],
    world: dict[str, Any],
    language_model: Any,
) -> tuple[D4Observation, list[str]]:
    """在模型参与路径上跑出用例观察。"""
    if language_model is None:
        raise AgentEvalError("model_mock requires a language_model")
    notes: list[str] = []
    request_oracle, employee, policy, transports, hotels = _world_models(world)
    provider, clock, malicious_refs, clock_box = _setup_provider_and_clock(
        case, world, transports, hotels
    )
    controls = world.get("execution_controls") or {}
    max_tool_calls = int(controls.get("tool_call_limit") or 12)
    max_clarification_rounds = int(controls.get("max_clarification_rounds") or 5)
    # Match production/demo city alias normalization (北京→Beijing, etc.).
    policy_configuration = load_policy_configuration()
    workflow = TripWorkflowOrchestrator(
        tasks=InMemoryTaskRepository(),
        employees=InMemoryEmployeeDirectory([employee]),
        policies=InMemoryPolicyRepository(policy),
        provider=provider,
        language_model=language_model,
        clock=clock,
        max_tool_calls=max_tool_calls,
        max_provider_attempts=2,
        max_llm_attempts=2,
        max_clarification_rounds=max_clarification_rounds,
        timezone_name=policy_configuration.config.timezone_name,
        city_normalizer=CityNormalizer(policy_configuration.city_aliases),
    )
    intent_message, remaining = planning_intent_message(case)
    if not intent_message:
        notes.append("no user turns for model intent extraction")
        return build_oracle_observation(case, world), notes
    notes.append(f"model intent message chars={len(intent_message)}")
    try:
        task = workflow.create_task_from_message(
            intent_message,
            traveler_id=employee.employee_id,
            task_id=case["case_id"],
        )
        task.tool_call_limit = max_tool_calls
    except Exception as exc:  # noqa: BLE001
        notes.append(f"create_task_from_message failed: {exc}")
        raise

    # Prefer the model-built request for post-search driving; fall back to oracle shape.
    request = task.request or request_oracle
    task, subject_matches = _drive_post_search(
        workflow=workflow,
        task=task,
        case=case,
        world=world,
        employee=employee,
        request=request,
        malicious_refs=malicious_refs,
        notes=notes,
        remaining_turns=remaining,
        clock_box=clock_box,
    )
    notes.append(f"real_model_calls={_count_llm_calls(task)}")
    return (
        _observation_from_task(
            task=task,
            case=case,
            world=world,
            employee=employee,
            policy=policy,
            request=request,
            malicious_refs=malicious_refs,
            subject_matches=subject_matches,
        ),
        notes,
    )


def run_case(
    case: dict[str, Any],
    world: dict[str, Any],
    *,
    mode: RunnerMode = "deterministic_live",
    language_model: Any | None = None,
    attempt: int = 1,
    price_table: Any | None = None,
) -> CaseRunResult:
    """运行单条 agent-eval 用例并返回结果。"""
    notes: list[str] = []
    error = None
    real_model_calls = 0
    llm_calls: tuple[dict[str, Any], ...] = ()
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    try:
        if mode == "oracle_label":
            observation = build_oracle_observation(case, world)
            notes.append("oracle_label mode")
        elif mode == "model_mock":
            observation, live_notes = run_model_observation(case, world, language_model)
            notes.extend(live_notes)
            real_model_calls = int(observation.extras.get("real_model_calls") or 0)
            fallback_model = str(getattr(language_model, "model", "") or "") or None
            priced = attach_costs_to_llm_calls(
                list(observation.extras.get("llm_calls") or ()),
                price_table=price_table,
                fallback_model=fallback_model,
            )
            llm_calls = tuple(priced)
            if priced:
                if all(item.get("input_tokens") is not None for item in priced):
                    input_tokens = sum(int(item["input_tokens"]) for item in priced)
                if all(item.get("output_tokens") is not None for item in priced):
                    output_tokens = sum(int(item["output_tokens"]) for item in priced)
                if all(item.get("estimated_cost_usd") is not None for item in priced):
                    estimated_cost_usd = round(
                        sum(float(item["estimated_cost_usd"]) for item in priced), 12
                    )
        else:
            observation, live_notes = run_live_observation(case, world)
            notes.extend(live_notes)
        assertions = evaluate_hard_assertions(case, observation)
        passed = all(item.passed for item in assertions)
        extras = observation.extras or {}
        return CaseRunResult(
            case_id=case["case_id"],
            category=case["category"],
            split=case["split"],
            mode=mode,
            passed=passed,
            final_state=observation.task.get("state"),
            policy_outcome=(observation.policy_oracle or {}).get("outcome"),
            assertion_results=assertions,
            tools_called=tuple(observation.tools_called),
            error=None,
            notes=tuple(notes),
            attempt=attempt,
            real_model_calls=real_model_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=estimated_cost_usd,
            llm_calls=llm_calls,
            intent_classification=extras.get("intent_classification"),
            intent_conflicts=tuple(extras.get("intent_conflicts") or ()),
            soft_conflicts=tuple(extras.get("soft_conflicts") or ()),
            missing_required_fields=tuple(extras.get("missing_required_fields") or ()),
            param_loop_final_action=extras.get("param_loop_final_action"),
            param_loop=extras.get("param_loop")
            if isinstance(extras.get("param_loop"), dict)
            else None,
            clarification_question=extras.get("clarification_question"),
        )
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        return CaseRunResult(
            case_id=case["case_id"],
            category=case["category"],
            split=case["split"],
            mode=mode,
            passed=False,
            final_state=None,
            policy_outcome=None,
            assertion_results=(),
            tools_called=(),
            error=error,
            notes=tuple(notes),
            attempt=attempt,
            real_model_calls=real_model_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=estimated_cost_usd,
            llm_calls=llm_calls,
        )


def load_agent_eval_subset(path: str | Path) -> dict[str, Any]:
    """加载用例子集清单。"""
    subset_path = Path(path)
    if not subset_path.is_file():
        raise AgentEvalError(f"subset file not found: {subset_path}")
    try:
        payload = json.loads(subset_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AgentEvalError(f"invalid subset JSON: {subset_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise AgentEvalError(f"subset root must be an object: {subset_path}")
    case_ids = payload.get("case_ids")
    records = payload.get("records")
    if not isinstance(case_ids, list) or not case_ids:
        raise AgentEvalError(f"subset missing non-empty case_ids: {subset_path}")
    if not all(isinstance(item, str) and item for item in case_ids):
        raise AgentEvalError(f"subset case_ids must be non-empty strings: {subset_path}")
    if len(set(case_ids)) != len(case_ids):
        raise AgentEvalError(f"subset case_ids contain duplicates: {subset_path}")
    if records is not None and records != len(case_ids):
        raise AgentEvalError(
            f"subset records={records} does not match case_ids count={len(case_ids)}: {subset_path}"
        )
    payload = dict(payload)
    payload["case_ids"] = list(case_ids)
    payload["_path"] = str(subset_path)
    payload["_sha256"] = hashlib.sha256(subset_path.read_bytes()).hexdigest()
    return payload


def run_agent_eval_dataset(
    directory: str | Path,
    *,
    mode: RunnerMode = "deterministic_live",
    case_ids: tuple[str, ...] | None = None,
    language_model: Any | None = None,
    attempts_per_case: int = 1,
    subset_id: str | None = None,
    price_table: Any | None = None,
) -> AgentEvalRunSummary:
    """批量运行 agent-eval 数据集并汇总。"""
    if attempts_per_case < 1:
        raise AgentEvalError("attempts_per_case must be at least 1")
    if mode == "model_mock" and language_model is None:
        raise AgentEvalError("model_mock mode requires language_model")
    cases, worlds, manifest = load_agent_eval_dataset(directory)
    selected = cases
    if case_ids:
        by_id = {case["case_id"]: case for case in cases}
        missing = [case_id for case_id in case_ids if case_id not in by_id]
        if missing:
            raise AgentEvalError(f"unknown case_ids: {sorted(missing)}")
        # Preserve caller/subset order for stable smoke reports.
        selected = [by_id[case_id] for case_id in case_ids]
    results_list: list[CaseRunResult] = []
    for case in selected:
        for attempt in range(1, attempts_per_case + 1):
            results_list.append(
                run_case(
                    case,
                    worlds[case["case_id"]],
                    mode=mode,
                    language_model=language_model,
                    attempt=attempt,
                    price_table=price_table,
                )
            )
    results = tuple(results_list)
    assertion_total = sum(len(item.assertion_results) for item in results)
    assertion_passed = sum(
        sum(1 for assertion in item.assertion_results if assertion.passed) for item in results
    )
    unique_cases = len(selected)
    # Multi-attempt: passed_cases = all attempts passed; error_cases = any attempt errored.
    by_case: dict[str, list[CaseRunResult]] = {}
    for item in results:
        by_case.setdefault(item.case_id, []).append(item)
    passed_cases = sum(
        1 for items in by_case.values() if items and all(item.passed for item in items)
    )
    error_cases = sum(1 for items in by_case.values() if any(item.error for item in items))
    failed_cases = max(0, unique_cases - passed_cases - error_cases)
    token_rows = [item for item in results if item.input_tokens is not None]
    cost_rows = [item for item in results if item.estimated_cost_usd is not None]
    return AgentEvalRunSummary(
        schema_version=1,
        dataset_id=manifest.get("dataset_id", "agent-eval-v1"),
        dataset_version=manifest.get("dataset_version", "unknown"),
        mode=mode,
        selected_cases=unique_cases,
        passed_cases=passed_cases,
        failed_cases=failed_cases,
        error_cases=error_cases,
        assertion_pass_rate=(assertion_passed / assertion_total if assertion_total else None),
        case_results=results,
        attempts_per_case=attempts_per_case,
        completed_runs=len(results),
        real_model_calls=sum(item.real_model_calls for item in results),
        subset_id=subset_id,
        runner_version=MODEL_MOCK_RUNNER_VERSION if mode == "model_mock" else None,
        input_tokens_total=(
            sum(int(item.input_tokens or 0) for item in token_rows) if token_rows else None
        ),
        output_tokens_total=(
            sum(int(item.output_tokens or 0) for item in token_rows) if token_rows else None
        ),
        estimated_cost_usd_total=(
            round(sum(float(item.estimated_cost_usd or 0.0) for item in cost_rows), 12)
            if cost_rows and len(cost_rows) == len(results)
            else (
                round(sum(float(item.estimated_cost_usd or 0.0) for item in cost_rows), 12)
                if cost_rows
                else None
            )
        ),
        currency=getattr(price_table, "currency", None) if price_table is not None else None,
        price_table_version=(
            getattr(price_table, "price_table_version", None) if price_table is not None else None
        ),
    )


def write_run_report(summary: AgentEvalRunSummary, output_directory: str | Path) -> Path:
    """将运行汇总写入报告目录。"""
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": summary.schema_version,
        "dataset_id": summary.dataset_id,
        "dataset_version": summary.dataset_version,
        "mode": summary.mode,
        "subset_id": summary.subset_id,
        "runner_version": summary.runner_version,
        "attempts_per_case": summary.attempts_per_case,
        "selected_cases": summary.selected_cases,
        "completed_runs": summary.completed_runs or len(summary.case_results),
        "passed_cases": summary.passed_cases,
        "failed_cases": summary.failed_cases,
        "error_cases": summary.error_cases,
        "real_model_calls": summary.real_model_calls,
        "input_tokens_total": summary.input_tokens_total,
        "output_tokens_total": summary.output_tokens_total,
        "estimated_cost_usd_total": summary.estimated_cost_usd_total,
        "currency": summary.currency,
        "price_table_version": summary.price_table_version,
        "assertion_pass_rate": summary.assertion_pass_rate,
        "case_results": [
            {
                "case_id": item.case_id,
                "category": item.category,
                "split": item.split,
                "mode": item.mode,
                "attempt": item.attempt,
                "passed": item.passed,
                "final_state": item.final_state,
                "policy_outcome": item.policy_outcome,
                "real_model_calls": item.real_model_calls,
                "input_tokens": item.input_tokens,
                "output_tokens": item.output_tokens,
                "estimated_cost_usd": item.estimated_cost_usd,
                "llm_calls": list(item.llm_calls),
                "tools_called": list(item.tools_called),
                "error": item.error,
                "notes": list(item.notes),
                "intent_classification": item.intent_classification,
                "intent_conflicts": list(item.intent_conflicts),
                "soft_conflicts": list(item.soft_conflicts),
                "missing_required_fields": list(item.missing_required_fields),
                "param_loop_final_action": item.param_loop_final_action,
                "param_loop": item.param_loop,
                "clarification_question": item.clarification_question,
                "failed_assertions": [
                    {
                        "assertion_id": assertion.assertion_id,
                        "kind": assertion.kind,
                        "expected": assertion.expected,
                        "actual": assertion.actual,
                        "operator": assertion.operator,
                        "path": assertion.path,
                        "message": assertion.message,
                    }
                    for assertion in item.assertion_results
                    if not assertion.passed
                ],
            }
            for item in summary.case_results
        ],
    }
    path = output / "agent-eval-v1-run-summary.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Per-call cost ledger (one JSON object per billable LLM call).
    ledger_path = output / "cost-ledger.jsonl"
    ledger_rows = 0
    with ledger_path.open("w", encoding="utf-8") as handle:
        for item in summary.case_results:
            for call in item.llm_calls:
                row = {
                    "case_id": item.case_id,
                    "attempt": item.attempt,
                    "passed": item.passed,
                    **call,
                }
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                ledger_rows += 1
    cost_summary = {
        "schema_version": 1,
        "currency": summary.currency or "USD",
        "price_table_version": summary.price_table_version,
        "unit": "per_1m_tokens",
        "real_model_calls": summary.real_model_calls,
        "ledger_rows": ledger_rows,
        "input_tokens_total": summary.input_tokens_total,
        "output_tokens_total": summary.output_tokens_total,
        "estimated_cost_usd_total": summary.estimated_cost_usd_total,
        "estimated_cost_usd_per_passed_case": (
            round(
                sum(
                    float(item.estimated_cost_usd or 0.0)
                    for item in summary.case_results
                    if item.passed and item.estimated_cost_usd is not None
                )
                / max(
                    1,
                    sum(
                        1
                        for item in summary.case_results
                        if item.passed and item.estimated_cost_usd is not None
                    ),
                ),
                12,
            )
            if any(
                item.passed and item.estimated_cost_usd is not None for item in summary.case_results
            )
            else None
        ),
        "notes": [
            "Costs use configured price-table rates; cached-input discounts are not modelled.",
            "Ledger rows are individual llm.extract_trip_intent calls with measured tokens.",
        ],
    }
    (output / "cost-summary.json").write_text(
        json.dumps(cost_summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    failures_path = output / "failed-assertions.jsonl"
    with failures_path.open("w", encoding="utf-8") as handle:
        for item in summary.case_results:
            if item.passed:
                continue
            for assertion in item.assertion_results:
                if assertion.passed:
                    continue
                handle.write(
                    json.dumps(
                        {
                            "case_id": item.case_id,
                            "assertion_id": assertion.assertion_id,
                            "expected": assertion.expected,
                            "actual": assertion.actual,
                            "message": assertion.message,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    return path
