"""记录：审计事件、评测轨迹、方案溯源、政策/预算/画像快照的读取。

从 `TripWorkflowOrchestrator` 按职责拆出来的一块；方法原文逐字来自拆分前的 orchestrator.py。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from corporate_travel_agent.agent.orchestrator.core import WorkflowError
from corporate_travel_agent.agent.ports import WorkflowTraceEvent
from corporate_travel_agent.domain.enums import PreferenceOrigin, TaskState
from corporate_travel_agent.domain.models import (
    BudgetSnapshot,
    EmployeeTravelProfileSnapshot,
    InventorySnapshot,
    PolicySnapshot,
    ProfilePreference,
    SearchProvenance,
    TravelOptionVersion,
    TripRequestVersion,
    TripTask,
)
from corporate_travel_agent.providers.base import HotelSearchQuery, TransportSearchQuery
from corporate_travel_agent.services.audit import new_audit_event
from corporate_travel_agent.services.budget_ledger import derive_budget_snapshot
from corporate_travel_agent.services.outbox_events import OutboxEventDraft
from corporate_travel_agent.services.provenance import option_provenance, provenance_hash
from corporate_travel_agent.services.travel_profile import derive_travel_profile


def _budget_snapshot_from_metadata(payload: dict) -> BudgetSnapshot:
    """从任务元数据里把钉住的预算快照读回来（金额和日期在 JSON 里是字符串）。"""
    return BudgetSnapshot(
        snapshot_id=str(payload["snapshot_id"]),
        cost_center=str(payload["cost_center"]),
        currency=str(payload["currency"]),
        limit=Decimal(str(payload["limit"])),
        spent=Decimal(str(payload["spent"])),
        period_from=date.fromisoformat(str(payload["period_from"])),
        period_to=date.fromisoformat(str(payload["period_to"])),
        computed_at=datetime.fromisoformat(str(payload["computed_at"])),
        source=str(payload.get("source", "booking_confirmations")),
    )


def _travel_profile_from_metadata(payload: dict) -> EmployeeTravelProfileSnapshot:
    """把钉在任务上的画像读回来。

    任务从数据库恢复时 metadata 是纯 JSON，`origin` 变回了字符串。这里显式还原成
    枚举——不还原的话，`planning/preferences.py` 里那张按枚举查的权重表会查不到，
    画像**静默失效**：不报错，只是排序悄悄变回没有画像的样子。
    """
    return EmployeeTravelProfileSnapshot(
        snapshot_id=payload["snapshot_id"],
        employee_id=payload["employee_id"],
        profile_version=payload.get("profile_version", 1),
        preferences=tuple(
            ProfilePreference(
                name=item["name"],
                origin=PreferenceOrigin(item["origin"]),
                evidence=item["evidence"],
            )
            for item in payload.get("preferences", ())
        ),
        preferred_hotels=tuple(payload.get("preferred_hotels", ())),
        cost_center=payload.get("cost_center"),
        derived_from_trips=payload.get("derived_from_trips", 0),
    )


class RecordsMixin:
    """记录：审计事件、评测轨迹、方案溯源、政策/预算/画像快照的读取。

    混入 `TripWorkflowOrchestrator`；状态都在宿主实例上，这里只放方法。
    """

    def _travel_profile(self, task: TripTask) -> EmployeeTravelProfileSnapshot | None:
        """这位员工的习惯画像；没接历史来源就是 None。

        **算出来就钉进任务**（`task.metadata["travel_profile"]`），此后这趟任务
        再规划多少次都用同一份。和政策快照同一个道理：员工下个月习惯变了，
        这趟任务"当初为什么这么排"仍然答得上来。

        算不出来不让整趟任务失败——画像只是排序上的加成，读历史出问题就当没有它，
        照常按这一轮说的话排。**这一层永远不该是任务失败的理由。**
        """
        if self.trip_history is None:
            return None
        pinned = task.metadata.get("travel_profile")
        if pinned is not None:
            return _travel_profile_from_metadata(pinned)
        try:
            profile = derive_travel_profile(task.employee, self.trip_history)
        except Exception:  # noqa: BLE001 — 见 docstring：画像失败不该拖垮任务
            self._audit(task, "TRAVEL_PROFILE_UNAVAILABLE", task.employee.employee_id, None)
            return None
        task.metadata["travel_profile"] = asdict(profile)
        self._audit(
            task,
            "TRAVEL_PROFILE_PINNED",
            profile.snapshot_id,
            [item.name for item in profile.preferences],
        )
        return profile

    def _budget_snapshot(self, task: TripTask) -> BudgetSnapshot | None:
        """这趟任务规划时的预算余额；没接账本、没成本中心或政策没配预算都是 None。

        **算出来就钉进任务**（`task.metadata["budget_snapshot"]`），和习惯画像同一个
        道理：别人下一秒确认了一单、余额变了，这趟"当初为什么这么判"仍然答得上来。
        算不出来（账本读失败）不让任务失败——那一刻规则判成"判不了"，请人定。
        """
        pinned = task.metadata.get("budget_snapshot")
        if pinned is not None:
            return _budget_snapshot_from_metadata(pinned)
        if self.budget_ledger is None:
            return None
        try:
            snapshot = derive_budget_snapshot(
                task.employee, self._policy_for(task), self.budget_ledger, now=self.clock()
            )
        except Exception:  # noqa: BLE001 — 见 docstring：账本失败不该拖垮任务
            self._audit(task, "BUDGET_SNAPSHOT_UNAVAILABLE", task.employee.employee_id, None)
            return None
        if snapshot is None:
            return None
        task.metadata["budget_snapshot"] = asdict(snapshot)
        self._audit(
            task,
            "BUDGET_SNAPSHOT_PINNED",
            snapshot.snapshot_id,
            {"remaining": str(snapshot.remaining), "currency": snapshot.currency},
        )
        return snapshot

    def _policy_for(self, task: TripTask) -> PolicySnapshot:
        """加载任务绑定的政策快照。"""
        try:
            policy = self.policies.snapshot(task.policy_snapshot_id)
        except KeyError as exc:
            raise WorkflowError("Historical policy snapshot is unavailable") from exc
        expected_hash = task.metadata.get("policy_content_hash")
        if expected_hash and expected_hash != policy.content_hash:
            raise WorkflowError("Historical policy snapshot content has changed")
        return policy

    @staticmethod
    def _request(task: TripTask) -> TripRequestVersion:
        if task.request is None:
            raise WorkflowError("The task does not have a complete TripRequestVersion")
        return task.request

    def _transition(self, task: TripTask, target: TaskState) -> None:
        """经状态机校验后迁移任务状态。"""
        previous = task.state
        task.state = self.state_machine.transition(previous, target)
        self._audit(task, "STATE_TRANSITION", previous.value, target.value)

    def _audit(
        self,
        task: TripTask,
        event_type: str,
        input_value: object,
        output_value: object,
        evidence_refs: tuple[str, ...] = (),
        *,
        outbox: Sequence[OutboxEventDraft] = (),
    ) -> None:
        """写审计事件并可选推送轨迹观察者。

        `outbox` 是要**随这次状态变化一起**进发件箱的事件：仓储把任务更新、审计事件和
        发件箱事件放进同一笔事务。审批单建了却没通知出去、或通知出去了审批单其实没建成，
        都是不能接受的半截。
        """
        event = new_audit_event(
            task.task_id,
            event_type,
            input_value=input_value,
            output_value=output_value,
            evidence_refs=evidence_refs,
        )
        if outbox:
            self.tasks.record(task, event, outbox_events=tuple(outbox))
        else:
            self.tasks.record(task, event)
        if event_type.startswith("TOOL_CALL_"):
            return
        state_before = task.state.value
        state_after = task.state.value
        if event_type == "STATE_TRANSITION":
            state_before = str(input_value)
            state_after = str(output_value)
        self._record_trace(
            WorkflowTraceEvent(
                kind=self._trace_kind(event_type),
                name=event_type,
                status="success",
                started_at=datetime.now(UTC),
                duration_ms=0.0,
                state_before=state_before,
                state_after=state_after,
                input_value=input_value,
                output_value=output_value,
                evidence_refs=evidence_refs,
            )
        )

    def _record_trace(self, event: WorkflowTraceEvent) -> None:
        """向轨迹观察者投递事件（若已配置）。"""
        if self.trace_observer is not None:
            self.trace_observer.record(event)

    @staticmethod
    def _trace_kind(event_type: str) -> str:
        if event_type == "STATE_TRANSITION":
            return "state_transition"
        if "RECOVER" in event_type or event_type.startswith("RETRY"):
            return "recovery"
        if event_type.startswith(("APPROVAL_", "OPTIONS_", "POLICY_")):
            return "policy"
        if event_type in {
            "TASK_CREATED_FROM_MESSAGE",
            "CLARIFICATION_RECEIVED",
            "STRUCTURED_FALLBACK_SUBMITTED",
        }:
            return "user"
        return "audit"

    @staticmethod
    def _trace_evidence_refs(result: object, input_value: object) -> tuple[str, ...]:
        refs: list[str] = []
        # `items` 这个名字在 InventorySnapshot 上是一串报价，在 dict 上却是个方法。
        # 工具循环的返回是 dict，直接迭代会炸——只认真正可迭代的那种。
        items = getattr(result, "items", ())
        if isinstance(items, (list, tuple)):
            for item in items:
                ref_id = getattr(item, "ref_id", None)
                if isinstance(ref_id, str):
                    refs.append(ref_id)
        if isinstance(result, dict):
            for option in result.get("options", ()) or ():
                ref_id = option.get("ref_id") if isinstance(option, dict) else None
                if isinstance(ref_id, str):
                    refs.append(ref_id)
        current_prices = getattr(result, "current_prices", None)
        if isinstance(current_prices, dict):
            refs.extend(str(key) for key in current_prices)
        refs.extend(str(item) for item in getattr(result, "unavailable_refs", ()))
        if isinstance(input_value, dict):
            for key in ("refs", "inventory_refs"):
                values = input_value.get(key, ())
                if isinstance(values, (list, tuple)):
                    refs.extend(str(item) for item in values)
        return tuple(dict.fromkeys(refs))

    def _pin_provenance(self, task: TripTask, option: TravelOptionVersion) -> None:
        """交接这一刻，把这条方案的依据链算一个哈希钉住。

        **钉的是哈希，不是那份记录本身。** 记录始终由已落库的不可变对象推导
        （`services/provenance.py`），另存一份就等于多一个会和事实分叉的事实源。
        存下指纹就够了：以后任何时候重算再比对，能改的地方都会露出来。
        """
        record = option_provenance(
            task=task,
            option=option,
            snapshots=self.tasks.snapshots(task.task_id),
            events=self.tasks.events(task.task_id),
            policy=self._policy_for(task),
        )
        digest = provenance_hash(record)
        task.metadata["provenance"] = {
            "option_id": option.option_id,
            "option_version": option.version,
            "hash": digest,
            "pinned_at": self.clock().isoformat(),
            # 缺口一并钉住：当时就说不清的地方，事后不该被悄悄补上。
            "gaps": list(record["gaps"]),
        }
        self._audit(task, "PROVENANCE_PINNED", option.option_id, digest)

    def verify_provenance(self, task_id: str) -> dict[str, Any]:
        """重算这条链，和交接时钉住的指纹比对。

        对得上，说明从交接到现在没有任何一环被改过；对不上，返回值会说清是
        哪一份记录变了——**它不自己修复，也不解释原因**，那是人要看的东西。
        """
        task = self.tasks.get(task_id)
        pinned = task.metadata.get("provenance")
        if not pinned:
            return {"status": "NOT_PINNED", "task_id": task_id}
        option = next(
            (item for item in task.options if item.option_id == pinned["option_id"]),
            None,
        )
        if option is None:
            return {"status": "OPTION_MISSING", "task_id": task_id, "pinned": pinned}
        record = option_provenance(
            task=task,
            option=option,
            snapshots=self.tasks.snapshots(task_id),
            events=self.tasks.events(task_id),
            policy=self._policy_for(task),
        )
        digest = provenance_hash(record)
        return {
            "status": "MATCH" if digest == pinned["hash"] else "MISMATCH",
            "task_id": task_id,
            "option_id": pinned["option_id"],
            "pinned_hash": pinned["hash"],
            "recomputed_hash": digest,
            "pinned_at": pinned["pinned_at"],
        }

    def option_provenance_record(self, task_id: str, option_id: str) -> dict[str, Any]:
        """取一条方案的依据链。任何时候都能取，不必等到交接。"""
        task = self.tasks.get(task_id)
        option = next(
            (item for item in task.options if item.option_id == option_id), None
        )
        if option is None:
            raise WorkflowError(f"Unknown option {option_id}")
        return option_provenance(
            task=task,
            option=option,
            snapshots=self.tasks.snapshots(task_id),
            events=self.tasks.events(task_id),
            policy=self._policy_for(task),
        )

    def _record_searches(
        self, task: TripTask, searches: Sequence[SearchProvenance]
    ) -> None:
        """把这一轮的搜索出处并进任务，按 snapshot_id 去重。

        去重是必要的：同一条任务会多轮搜索，重验也会再走一次，而快照是不可变的
        ——同一个 snapshot_id 的出处不该记两遍。
        """
        known = {item.snapshot_id for item in task.searches}
        for item in searches:
            if item.snapshot_id in known:
                continue
            known.add(item.snapshot_id)
            task.searches.append(item)

    @staticmethod
    def _transport_provenance(
        query: TransportSearchQuery, snapshot: InventorySnapshot
    ) -> SearchProvenance:
        """结构化入口的出处。

        这条路上**没有用户原话可抄**——日期来自已经校验过的 `TripRequestVersion`，
        不是从一句话里读出来的。`date_evidence` 因此留空，如实反映"这一天的依据
        在请求版本里，不在对话里"。
        """
        return SearchProvenance(
            kind="transport",
            parameters=(
                ("origin", query.origin),
                ("destination", query.destination),
                ("depart_after", query.depart_after.isoformat()),
                ("arrive_by", query.arrive_before.isoformat()),
            ),
            snapshot_id=snapshot.snapshot_id,
            query_hash=snapshot.query_hash,
            captured_at=snapshot.captured_at,
            valid_until=snapshot.valid_until,
        )

    @staticmethod
    def _hotel_provenance(
        query: HotelSearchQuery, snapshot: InventorySnapshot
    ) -> SearchProvenance:
        return SearchProvenance(
            kind="hotel",
            parameters=(
                ("city", query.city),
                ("check_in", query.check_in.isoformat()),
                ("check_out", query.check_out.isoformat()),
            ),
            snapshot_id=snapshot.snapshot_id,
            query_hash=snapshot.query_hash,
            captured_at=snapshot.captured_at,
            valid_until=snapshot.valid_until,
        )
