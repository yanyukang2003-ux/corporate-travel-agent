"""规划：搜索与可行性、无可行方案的解释、报价重验、重试/重规划、选方案。

从 `TripWorkflowOrchestrator` 按职责拆出来的一块；方法原文逐字来自拆分前的 orchestrator.py。
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import partial
from typing import cast
from uuid import uuid4

from corporate_travel_agent.agent.error_recovery import (
    classify_tool_failure,
    hotel_search_tool_name,
    recon_search_legs,
    transport_search_tool_name,
)
from corporate_travel_agent.agent.orchestrator.core import (
    PARTIAL_COVERAGE_METADATA_KEY,
    ToolBudgetExceeded,
    WorkflowError,
)
from corporate_travel_agent.agent.orchestrator.recorder import (
    hotel_provenance,
    transport_provenance,
)
from corporate_travel_agent.agent.orchestrator.state import OrchestratorState
from corporate_travel_agent.domain.enums import (
    ApprovalStatus,
    PolicyOutcome,
    RevalidationStatus,
    TaskState,
)
from corporate_travel_agent.domain.models import (
    BookingIntent,
    HotelOffer,
    InventorySnapshot,
    PolicySnapshot,
    SearchProvenance,
    TransportOffer,
    TripRequestVersion,
    TripTask,
)
from corporate_travel_agent.planning.feasibility import leg_spec, planned_leg_count
from corporate_travel_agent.policy.engine import unreviewable_reasons
from corporate_travel_agent.providers.base import (
    HotelSearchQuery,
    JourneySearchQuery,
    MultiCityInventoryProvider,
    ProviderError,
    RetryableProviderError,
    TransportSearchQuery,
)
from corporate_travel_agent.services.audit import stable_hash
from corporate_travel_agent.services.provider_resilience import PROVIDER_RETRY_METADATA_KEY


def _fares_from(offers: list[TransportOffer]) -> list[list[TransportOffer]]:
    """把一次整票搜索的结果按 ``fare_ref`` 分回一张张票。

    一张票的几段在快照里是并排放着的；分组之后每一组就是**一个不可拆的候选**。
    没有 ``fare_ref`` 的（理论上不会出现在整票快照里）各自成一组，不会被静默丢掉。
    """
    grouped: dict[str, list[TransportOffer]] = {}
    order: list[str] = []
    for offer in offers:
        key = offer.fare_ref or offer.ref_id
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(offer)
    return [grouped[key] for key in order]


def _leg_phrase(request: TripRequestVersion, leg_index: int) -> str:
    """报错文案里怎么称呼这一段。

    沿用旧名字的那两段（outbound、以及往返里的 return）说法一个字不变——
    既有文案、评测断言与前端都认这两个词。**其余段一律带上自己的航线**：
    多城行程里光说"第 1 段没搜到"看的人根本不知道是哪一程，
    而这正是中间段搜不到时唯一能给出的线索。

    按**标签**判而不是按下标判：三段行程的第 1 段是中途那一段，不是返程，
    `leg_spec` 已经叫它 "leg 1" 了——那它就该带航线。
    """
    spec = leg_spec(request, leg_index)
    if spec.label in {"outbound", "return"}:
        return spec.label
    return f"{spec.label} ({spec.origin} → {spec.destination})"


class PlanningMixin(OrchestratorState):
    """规划：搜索与可行性、无可行方案的解释、报价重验、重试/重规划、选方案。

    混入 `TripWorkflowOrchestrator`；状态都在宿主实例上，这里只放方法。
    """

    def _search_and_plan(self, task: TripTask, policy: PolicySnapshot) -> TripTask:
        """调用 Provider 搜库存，规划可行方案并迁移状态。"""
        self.recorder.transition(task, TaskState.SEARCHING)
        task.failure = None
        task.metadata.pop("no_feasible_reasons", None)
        request = self._request(task)
        transport_legs = request.transport_legs()
        # 一段交通一次、一站住宿一次。此前住宿无论几站都只算一次，
        # 多城行程会在预算够两次时开搜、搜到第二站才发现调用用光。
        stays = request.lodging_stays()
        # 两段起再加一次"整票"搜索：一次请求问完整条行程，供应商按 IATA 票价构造
        # 规则给一个覆盖全程的价——**不是几张单程相加**。实测整票便宜 15%–76%，
        # 连普通往返都便宜 18%–23%（`reports/evaluation-runs/multicity-pricing-*/`）。
        # 整票和分段购买是两种真正不同的走法，一起摆出来由人取舍。
        journey_provider = (
            self.provider if isinstance(self.provider, MultiCityInventoryProvider) else None
        )
        wants_journey_fare = (
            len(transport_legs) >= self.journey_fare_min_legs and journey_provider is not None
        )
        required_calls = len(transport_legs) + len(stays) + int(wants_journey_fare)
        if task.tool_calls_remaining < required_calls:
            return self._stop_for_tool_budget(
                task,
                "provider.search_inventory",
                required_calls=required_calls,
            )
        if not self._prepare_provider_operation(task, resume_operation="SEARCH"):
            return task
        try:
            # 一段一次搜索，段数由行程自己说了算。此前这里是写死的"去程一次、返程一次"
            # ——第三段没有位置可搜，规划器再能规划多城也拿不到货。
            leg_snapshots: list[InventorySnapshot] = []
            #: 每段的查询留一份，好让搜完能说出"这个快照是拿什么参数搜来的"。
            searched: list[SearchProvenance] = []
            for index, leg in enumerate(transport_legs):
                leg_query = TransportSearchQuery(
                    origin=leg.origin,
                    destination=leg.destination,
                    depart_after=leg.depart_after,
                    arrive_before=leg.arrive_before,
                )
                leg_snapshots.append(
                    self._invoke_tool(
                        task,
                        tool_name=transport_search_tool_name(index),
                        tool_kind="PROVIDER",
                        input_value=leg_query,
                        # 每轮各自绑住自己的 query：闭包晚绑定会让所有段都搜最后一段。
                        operation=partial(self.provider.search_transport, leg_query),
                    )
                )
                searched.append(
                    transport_provenance(leg_query, leg_snapshots[-1])
                )
            journey_snapshot: InventorySnapshot | None = None
            if wants_journey_fare and journey_provider is not None:
                leg_queries = [
                    TransportSearchQuery(
                        origin=leg.origin,
                        destination=leg.destination,
                        depart_after=leg.depart_after,
                        arrive_before=leg.arrive_before,
                    )
                    for leg in transport_legs
                ]
                try:
                    journey_snapshot = self._invoke_tool(
                        task,
                        tool_name="provider.search_transport.journey",
                        tool_kind="PROVIDER",
                        input_value=JourneySearchQuery(tuple(leg_queries)),
                        operation=partial(journey_provider.search_multi_city, leg_queries),
                    )
                except ProviderError as exc:
                    # **整票搜不到不算失败。** 它是一条额外的、更便宜的走法；
                    # 供应商给不出来就退回分段购买——少省一笔钱，不是这趟走不了。
                    # 分段那几次搜索已经成功，不该被这一次拖垮。
                    task.metadata["journey_fare_unavailable"] = str(exc)[:200]

            # 一站一次搜索。此前只搜一次，城市写死是 `request.destination`
            # ——多城行程的第二站根本没有被搜过。
            hotel_snapshots: list[InventorySnapshot] = []
            for index, stay in enumerate(stays):
                hotel_query = HotelSearchQuery(
                    city=stay.city,
                    check_in=stay.check_in,
                    check_out=stay.check_out,
                )
                hotel_snapshots.append(
                    self._invoke_tool(
                        task,
                        tool_name=hotel_search_tool_name(index),
                        tool_kind="PROVIDER",
                        input_value=hotel_query,
                        operation=partial(self.provider.search_hotels, hotel_query),
                    )
                )
                searched.append(
                    hotel_provenance(hotel_query, hotel_snapshots[-1])
                )
        except ToolBudgetExceeded:
            self.provider_circuit_breaker.record_success()
            return self._stop_for_tool_budget(task, "provider.search_inventory")
        except ProviderError as exc:
            # Multi-leg recon: outbound may have succeeded before inbound/hotel failed.
            leg_recon = recon_search_legs(task.tool_calls)
            decision = classify_tool_failure(
                tool_name=str(leg_recon.get("recovery", {}).get("failed_leg") or "provider.search"),
                tool_kind="PROVIDER",
                retryable=isinstance(exc, RetryableProviderError),
                response_received=getattr(exc, "response_received", None),
                attempt=self.max_provider_attempts,
                max_attempts=self.max_provider_attempts,
                will_auto_retry=False,
            )
            # Prefer recon action when any leg already succeeded (partial search).
            if leg_recon.get("partial") or leg_recon.get("started_legs"):
                recovery_payload = leg_recon["recovery"]
            else:
                recovery_payload = decision.as_dict()
            task.failure = str(exc)
            task.metadata["search_failure_recon"] = leg_recon
            task.metadata["last_recovery"] = recovery_payload
            # Do not keep partial options from an aborted multi-leg search.
            task.options = []
            self.recorder.audit(
                task,
                "PROVIDER_SEARCH_ATTEMPTS_EXHAUSTED",
                request,
                {"failure": task.failure, "recovery": recovery_payload, "recon": leg_recon},
            )
            if isinstance(exc, RetryableProviderError):
                self.provider_circuit_breaker.record_failure()
                self._schedule_provider_retry(
                    task,
                    resume_operation="SEARCH",
                    error=exc,
                )
                return task
            self.provider_circuit_breaker.record_success()
            self._mark_provider_retry_terminal(task, status="failed")
            self.recorder.transition(task, TaskState.PROVIDER_FAILED)
            return task

        self.provider_circuit_breaker.record_success()
        snapshots = [
            *leg_snapshots,
            *([journey_snapshot] if journey_snapshot is not None else []),
            *hotel_snapshots,
        ]
        invalid_snapshots = self._invalid_snapshot_ids(snapshots)
        if invalid_snapshots:
            task.failure = "provider returned expired or invalid inventory snapshots: " + ", ".join(
                invalid_snapshots
            )
            self._mark_provider_retry_terminal(task, status="failed")
            self.recorder.transition(task, TaskState.PROVIDER_FAILED)
            self.recorder.audit(
                task,
                "INVENTORY_SNAPSHOT_REJECTED",
                tuple(invalid_snapshots),
                task.failure,
            )
            return task
        self._complete_provider_retry(task)
        self.recorder.record_searches(task, searched)
        self._record_coverage_notices(task, snapshots)
        self._audit_snapshots(task, snapshots)
        self.recorder.transition(task, TaskState.PLANNING)

        task.options = self.planner.plan(
            request=request,
            employee=task.employee,
            policy=policy,
            leg_offers=[
                self._without_excluded(task, self._transports(item)) for item in leg_snapshots
            ],
            journey_fares=[
                fare
                for fare in _fares_from(self._transports(journey_snapshot))
                if len(self._without_excluded(task, fare)) == len(fare)
            ],
            hotel_offers=[self._hotels(item) for item in hotel_snapshots],
            profile=self._travel_profile(task),
            budget=self._budget_snapshot(task),
            now=self.clock(),
        )
        if not task.options:
            reasons = self._no_feasible_reasons(
                request,
                leg_snapshots,
                hotel_snapshots,
                policy=policy,
            )
            task.metadata["no_feasible_reasons"] = reasons
            task.failure = "; ".join(reasons)
            self.recorder.transition(task, TaskState.NO_FEASIBLE_OPTION)
            self.recorder.audit(task, "NO_FEASIBLE_OPTION", request.version, reasons)
            return task

        self.recorder.transition(task, TaskState.OPTIONS_READY)
        self.recorder.audit(
            task,
            "OPTIONS_VERIFIED",
            request.version,
            [item.option_id for item in task.options],
            tuple(ref for item in task.options for ref in item.inventory_refs),
        )
        self.recorder.transition(task, TaskState.WAITING_FOR_USER)
        return task

    def _no_feasible_reasons(
        self,
        request: TripRequestVersion,
        leg_snapshots: Sequence[InventorySnapshot],
        hotel_snapshots: Sequence[InventorySnapshot],
        *,
        policy: PolicySnapshot | None = None,
    ) -> tuple[str, ...]:
        """汇总无可行动方案的原因文案。"""
        reasons: list[str] = []
        leg_pools = [self._transports(item) for item in leg_snapshots]
        stay_pools = [self._hotels(item) for item in hotel_snapshots]
        hotels = [offer for pool in stay_pools for offer in pool]
        for index in range(planned_leg_count(request)):
            snapshot = leg_snapshots[index] if index < len(leg_snapshots) else None
            if index < len(leg_pools) and leg_pools[index]:
                continue
            reasons.append(
                self._time_window_filter_reason(snapshot)
                or f"no {_leg_phrase(request, index)} inventory matched the requested "
                "route and time window"
            )
        for index, stay in enumerate(request.lodging_stays()):
            if index < len(stay_pools) and stay_pools[index]:
                continue
            # 第一站的说法一个字没动；第二站起才把城市名写进话里。
            reasons.append(
                "no hotel inventory matched the requested city and dates"
                if index == 0
                else f"no hotel inventory matched {stay.city} for the requested dates"
            )
        if reasons:
            return tuple(reasons)

        # Inventory exists but every combination failed feasibility/policy.
        # Surface the dominant evidence gaps so operators are not left with a
        # generic message (e.g. USD flight + CNY hotel → currency evidence).
        if policy is not None:
            priced_currencies = sorted(
                {
                    *(offer.currency for pool in leg_pools for offer in pool),
                    *(item.currency for item in hotels),
                }
            )
            if priced_currencies and priced_currencies != [policy.currency]:
                reasons.append(
                    "inventory currencies "
                    f"[{', '.join(priced_currencies)}] cannot be combined under policy "
                    f"currency {policy.currency} without an approved FX snapshot "
                    "(pricing.currency → INSUFFICIENT_EVIDENCE)"
                )
            if hotels:
                unknown_cap_cities = sorted(
                    {
                        hotel.city
                        for hotel in hotels
                        if hotel.currency == policy.currency
                        and hotel.city not in policy.hotel_city_caps
                    }
                )
                if unknown_cap_cities:
                    reasons.append(
                        "no hotel nightly cap is configured for cities: "
                        + ", ".join(unknown_cap_cities)
                        + " (hotel.city.nightly_cap → INSUFFICIENT_EVIDENCE)"
                    )
            # Sample a few combos for feasibility / policy rejection codes.
            sample_reasons = self._sample_itinerary_rejection_reasons(
                request,
                policy,
                leg_pools[: planned_leg_count(request)],
                stay_pools[: len(request.lodging_stays())],
            )
            reasons.extend(sample_reasons)
        if not reasons:
            reasons.append(
                "inventory was found, but every complete itinerary failed a hard "
                "constraint or policy evidence requirement"
            )
        return tuple(dict.fromkeys(reasons))

    @staticmethod
    def _time_window_filter_reason(snapshot: InventorySnapshot | None) -> str | None:
        if snapshot is None:
            return None
        blob = " ".join(snapshot.provider_warnings)
        if "filtered_by_arrive_before" in blob:
            return (
                "supplier returned flights, but all arrived after the requested "
                "arrive_by window"
            )
        if "filtered_by_depart_after" in blob:
            return (
                "supplier returned flights, but all departed before the requested "
                "departure_after window"
            )
        if "filtered_expired" in blob:
            return "supplier returned flights, but every offer had already expired"
        return None

    def _sample_itinerary_rejection_reasons(
        self,
        request: TripRequestVersion,
        policy: PolicySnapshot,
        leg_pools: Sequence[list[TransportOffer]],
        stay_pools: Sequence[list[HotelOffer]],
        *,
        sample_limit: int = 3,
        combination_limit: int = 64,
    ) -> list[str]:
        """Return a few concrete rejection codes from sampled flight×hotel combos.

        只在**每段都有货**的前提下才走到这里（上面为空的段已经各自出过话），
        所以直接按段取样即可。段数一多组合数是指数的，用 ``combination_limit``
        兜住——这是给运维看的取样，不是穷举。
        """
        from itertools import islice, product

        from corporate_travel_agent.planning.feasibility import FeasibilityValidator
        from corporate_travel_agent.policy.engine import PolicyEngine
        from corporate_travel_agent.services.repositories import NotFoundError

        sampled_legs = [pool[:sample_limit] for pool in leg_pools if pool]
        if not sampled_legs:
            return []
        sampled_stays = [pool[:sample_limit] for pool in stay_pools if pool]
        try:
            employee = self.tasks.get(request.task_id).employee
        except NotFoundError:
            employee = self.employees.snapshot(request.traveler_id)

        validator = FeasibilityValidator()
        engine = PolicyEngine()
        counts: dict[str, int] = {}
        leg_count = len(sampled_legs)
        combinations = product(*sampled_legs, *sampled_stays)
        for combination in islice(combinations, combination_limit):
            # product() 混合了两种候选池，静态类型退化成 object；按位置切开后类型是确定的。
            transports = cast(list[TransportOffer], list(combination[:leg_count]))
            hotels = cast(list[HotelOffer], list(combination[leg_count:]))
            feasibility = validator.validate(
                request,
                transports,
                hotels,
                policy.arrival_buffer_minutes,
                now=self.clock(),
            )
            if not feasibility.feasible:
                for reason in feasibility.reasons:
                    key = f"feasibility:{reason}"
                    counts[key] = counts.get(key, 0) + 1
                continue
            decision = engine.evaluate(employee, policy, transports, hotels, now=self.clock())
            if decision.outcome in {
                PolicyOutcome.FORBIDDEN,
                PolicyOutcome.INSUFFICIENT_EVIDENCE,
            }:
                for item in decision.evidence:
                    if item.outcome in {
                        PolicyOutcome.FORBIDDEN,
                        PolicyOutcome.INSUFFICIENT_EVIDENCE,
                    }:
                        key = f"policy:{item.rule_id}={item.outcome.value}"
                        counts[key] = counts.get(key, 0) + 1
        ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
        return [f"{code} (x{count})" for code, count in ranked[:4]]

    def _revalidate_selected(self, task: TripTask) -> TripTask:
        """对选中方案再校验价格/可用性，再生成 Booking Intent。"""
        option = task.selected_option()
        if option is None:
            raise WorkflowError("No selected option")
        if task.tool_calls_remaining < 1:
            return self._stop_for_tool_budget(task, "provider.revalidate")
        if not self._prepare_provider_operation(task, resume_operation="REVALIDATE"):
            return task
        try:
            result = self._invoke_tool(
                task,
                tool_name="provider.revalidate",
                tool_kind="PROVIDER",
                input_value={"refs": option.inventory_refs},
                operation=lambda: self.provider.revalidate(option.inventory_refs),
            )
        except ToolBudgetExceeded:
            self.provider_circuit_breaker.record_success()
            return self._stop_for_tool_budget(task, "provider.revalidate")
        except ProviderError as exc:
            task.failure = str(exc)
            self.recorder.audit(
                task,
                "PROVIDER_REVALIDATION_ATTEMPTS_EXHAUSTED",
                option.inventory_refs,
                task.failure,
            )
            if isinstance(exc, RetryableProviderError):
                self.provider_circuit_breaker.record_failure()
                self._schedule_provider_retry(
                    task,
                    resume_operation="REVALIDATE",
                    error=exc,
                )
                return task
            self.provider_circuit_breaker.record_success()
            self._mark_provider_retry_terminal(task, status="failed")
            self.recorder.transition(task, TaskState.PROVIDER_FAILED)
            return task
        self.provider_circuit_breaker.record_success()
        self.recorder.audit(task, "INVENTORY_REVALIDATED", option.inventory_refs, result)
        if result.status is RevalidationStatus.PROVIDER_FAILED:
            task.failure = "; ".join(result.warnings)
            self._mark_provider_retry_terminal(task, status="failed")
            self.recorder.transition(task, TaskState.PROVIDER_FAILED)
            return task
        if result.status in {
            RevalidationStatus.PRICE_CHANGED,
            RevalidationStatus.UNAVAILABLE,
        }:
            self._complete_provider_retry(task)
            self.recorder.transition(task, TaskState.RECONFIRMATION_REQUIRED)
            return task

        idempotency_key = stable_hash(
            {
                "task_id": task.task_id,
                "request_version": self._request(task).version,
                "option_id": option.option_id,
                "option_version": option.version,
            }
        )
        if task.booking_intent is None:
            try:
                handoff = self._invoke_tool(
                    task,
                    tool_name="provider.create_deep_link",
                    tool_kind="PROVIDER",
                    input_value={
                        "option_id": option.option_id,
                        "option_version": option.version,
                        "inventory_refs": option.inventory_refs,
                    },
                    operation=lambda: self.provider.create_deep_link(option),
                )
            except ToolBudgetExceeded:
                self.provider_circuit_breaker.record_success()
                return self._stop_for_tool_budget(task, "provider.create_deep_link")
            except ProviderError as exc:
                task.failure = str(exc)
                self.recorder.audit(
                    task,
                    "PROVIDER_HANDOFF_ATTEMPTS_EXHAUSTED",
                    option.option_id,
                    task.failure,
                )
                if isinstance(exc, RetryableProviderError):
                    self.provider_circuit_breaker.record_failure()
                    self._schedule_provider_retry(
                        task,
                        resume_operation="REVALIDATE",
                        error=exc,
                    )
                    return task
                self.provider_circuit_breaker.record_success()
                self._mark_provider_retry_terminal(task, status="failed")
                self.recorder.transition(task, TaskState.PROVIDER_FAILED)
                return task
            if not self._timezone_aware(handoff.expires_at) or handoff.expires_at <= self.clock():
                task.failure = "provider returned an expired or invalid handoff"
                self._mark_provider_retry_terminal(task, status="failed")
                self.recorder.transition(task, TaskState.PROVIDER_FAILED)
                self.recorder.audit(
                    task,
                    "PROVIDER_HANDOFF_REJECTED",
                    option.option_id,
                    task.failure,
                )
                return task
            task.booking_intent = BookingIntent(
                intent_id=str(uuid4()),
                idempotency_key=idempotency_key,
                selected_option_id=option.option_id,
                selected_option_version=option.version,
                handoff=handoff,
                revalidated_at=result.checked_at,
            )
        elif task.booking_intent.idempotency_key != idempotency_key:
            raise WorkflowError("INV-006: conflicting BookingIntent")
        self.provider_circuit_breaker.record_success()
        self._complete_provider_retry(task)
        self._pin_provenance(task, option)
        self.recorder.transition(task, TaskState.READY_FOR_HANDOFF)
        self.recorder.audit(
            task, "BOOKING_INTENT_CREATED", idempotency_key, task.booking_intent.intent_id
        )
        return task

    def retry_or_replan(self, task_id: str) -> TripTask:
        """在无可行方案或 Provider 失败后按策略重试/重规划。"""
        task = self.tasks.get(task_id)
        prior_state = task.state
        if task.state not in {
            TaskState.PROVIDER_FAILED,
            TaskState.NO_FEASIBLE_OPTION,
            TaskState.RECONFIRMATION_REQUIRED,
            TaskState.WAITING_FOR_PROVIDER,
        }:
            raise WorkflowError(f"Cannot replan task in {task.state.value}")
        if task.approval and task.approval.status is ApprovalStatus.APPROVED:
            task.approval.status = ApprovalStatus.INVALIDATED
            self.recorder.audit(task, "APPROVAL_INVALIDATED", task.approval.subject_hash, "replan")
        task.selected_option_id = None
        retry_metadata = self._provider_retry_metadata(task)
        if prior_state is TaskState.WAITING_FOR_PROVIDER and retry_metadata is not None:
            retry_metadata["status"] = "scheduled"
            retry_metadata["next_retry_at"] = self.clock().isoformat()
            retry_metadata["trigger"] = "manual"
            self.recorder.audit(
                task,
                "PROVIDER_DELAYED_RETRY_REQUESTED",
                {"trigger": "manual"},
                self._public_provider_retry_metadata(retry_metadata),
            )
        elif retry_metadata is not None:
            task.metadata.pop(PROVIDER_RETRY_METADATA_KEY, None)
            self.recorder.audit(
                task,
                "PROVIDER_RETRY_CYCLE_RESET",
                {"trigger": "manual_replan", "from_state": prior_state.value},
                {"previous_status": retry_metadata.get("status")},
            )
        return self._search_and_plan(task, self._policy_for(task))

    def select_option(
        self,
        task_id: str,
        option_id: str,
        *,
        business_reason: str | None = None,
    ) -> TripTask:
        """用户选中某方案，进入审批或再校验/交接路径。"""
        task = self.tasks.get(task_id)
        if task.state is not TaskState.WAITING_FOR_USER:
            raise WorkflowError(f"Cannot select an option in {task.state.value}")
        option = next((item for item in task.options if item.option_id == option_id), None)
        if option is None:
            raise WorkflowError(f"Unknown option {option_id}")
        if not option.feasibility.feasible:
            raise WorkflowError("INV-002: an infeasible option cannot be selected")
        decision = option.policy_decision
        # **"禁止"要在最前面挡住，不能只看聚合结论。** 政策引擎的严重度是
        # "证据不足 > 禁止"，所以一条既违规又缺数据的方案聚合出来是
        # INSUFFICIENT_EVIDENCE；而那一档现在是可选的（走人工审批），
        # 只认 outcome 的话，被禁的方案会从"判不了"这道门溜出去。
        if decision.forbidden_rule_ids:
            raise WorkflowError("INV-003: a forbidden option cannot proceed")
        # 判不了、又拿不出可批材料的方案同样不能选：批的人看不出自己在批什么。
        unreviewable = unreviewable_reasons(decision)
        if unreviewable:
            raise WorkflowError(
                f"INV-003: an option with no reviewable policy evidence cannot proceed "
                f"({', '.join(unreviewable)})"
            )
        # 需审批和证据不足都要人来定，但定的是两件事：前者是"违规但值得破例吗"，
        # 后者是"系统查不到公司的标准，请你确认"。走同一条审批路径，
        # 材料里分开写（见 `_new_approval` 的 violations）。
        needs_human_judgment = decision.outcome in {
            PolicyOutcome.REQUIRES_APPROVAL,
            PolicyOutcome.INSUFFICIENT_EVIDENCE,
        }
        if needs_human_judgment and (not business_reason or not business_reason.strip()):
            raise WorkflowError("A business reason is required for a policy exception")

        task.selected_option_id = option_id
        self.recorder.audit(task, "OPTION_SELECTED", option_id, decision.outcome.value)
        if needs_human_judgment:
            assert business_reason is not None
            task.approval = self._new_approval(task, business_reason.strip())
            self.recorder.transition(task, TaskState.WAITING_FOR_APPROVAL)
            self.recorder.audit(
                task,
                "APPROVAL_REQUESTED",
                option_id,
                task.approval.subject_hash,
                option.inventory_refs,
                outbox=(self._approval_event(task, "APPROVAL_REQUESTED"),),
            )
            return task
        if decision.outcome is not PolicyOutcome.COMPLIANT:
            raise WorkflowError("INV-003: non-compliant option cannot proceed")

        self.recorder.transition(task, TaskState.REVALIDATING)
        return self._revalidate_selected(task)

    def _record_coverage_notices(self, task: TripTask, snapshots: list[InventorySnapshot]) -> None:
        """Carry provider-declared coverage caveats through to the user.

        A snapshot that a provider labels incomplete must never be presented as
        a full search. Silently dropping these warnings makes an incomplete
        result look exhaustive, so they are preserved as evidence-linked
        notices rather than folded into failure text.
        """

        notices = tuple(
            dict.fromkeys(
                f"{snapshot.snapshot_id}: {warning}"
                for snapshot in snapshots
                for warning in snapshot.provider_warnings
            )
        )
        if notices:
            task.metadata[PARTIAL_COVERAGE_METADATA_KEY] = notices
            self.recorder.audit(
                task, "PROVIDER_COVERAGE_INCOMPLETE", tuple(notices), task.state.value
            )
        else:
            task.metadata.pop(PARTIAL_COVERAGE_METADATA_KEY, None)

    def _audit_snapshots(self, task: TripTask, snapshots: list[InventorySnapshot]) -> None:
        for snapshot in snapshots:
            self.tasks.add_snapshot(task.task_id, snapshot)
            self.recorder.audit(
                task,
                "INVENTORY_SNAPSHOT_CAPTURED",
                snapshot.query_hash,
                snapshot.raw_payload_hash,
                tuple(item.ref_id for item in snapshot.items),
            )

    def _invalid_snapshot_ids(self, snapshots: list[InventorySnapshot]) -> list[str]:
        observed_at = self.clock()
        return [
            snapshot.snapshot_id
            for snapshot in snapshots
            if not self._timezone_aware(snapshot.captured_at)
            or not self._timezone_aware(snapshot.valid_until)
            or snapshot.valid_until <= snapshot.captured_at
            or snapshot.valid_until <= observed_at
        ]

    @staticmethod
    def _transports(snapshot: InventorySnapshot | None) -> list[TransportOffer]:
        if snapshot is None:
            return []
        return [item for item in snapshot.items if isinstance(item, TransportOffer)]

    @staticmethod
    def _without_excluded(task: TripTask, offers: list[TransportOffer]) -> list[TransportOffer]:
        """改期任务：航司说变了/没了的那张票不再端上来（`metadata["excluded_refs"]`）。"""
        excluded = set(task.metadata.get("excluded_refs") or ())
        if not excluded:
            return offers
        return [item for item in offers if item.ref_id not in excluded]

    @staticmethod
    def _hotels(snapshot: InventorySnapshot | None) -> list[HotelOffer]:
        if snapshot is None:
            return []
        return [item for item in snapshot.items if isinstance(item, HotelOffer)]
