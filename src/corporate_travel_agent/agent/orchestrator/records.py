"""记录的**读**侧：政策/预算/画像快照的解析与钉住、方案溯源。

写侧（审计事件、状态迁移、轨迹、搜索出处）2026-09-03 起是独立的协作对象 `recorder.TaskRecorder`
（ADR-0006）；这里剩下的方法只读仓储、只改任务上的 metadata，需要落库时调 `self.recorder.audit`。
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from corporate_travel_agent.agent.orchestrator.core import WorkflowError
from corporate_travel_agent.agent.orchestrator.state import OrchestratorState
from corporate_travel_agent.domain.enums import PreferenceOrigin
from corporate_travel_agent.domain.models import (
    BudgetSnapshot,
    EmployeeTravelProfileSnapshot,
    PolicySnapshot,
    ProfilePreference,
    TravelOptionVersion,
    TripRequestVersion,
    TripTask,
)
from corporate_travel_agent.services.budget_ledger import derive_budget_snapshot
from corporate_travel_agent.services.provenance import option_provenance, provenance_hash
from corporate_travel_agent.services.travel_profile import derive_travel_profile


def _budget_snapshot_from_metadata(payload: dict[str, Any]) -> BudgetSnapshot:
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


def _travel_profile_from_metadata(payload: dict[str, Any]) -> EmployeeTravelProfileSnapshot:
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


class RecordsMixin(OrchestratorState):
    """快照解析与溯源；混入 `TripWorkflowOrchestrator`，状态都在宿主实例上。"""

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
            self.recorder.audit(task, "TRAVEL_PROFILE_UNAVAILABLE", task.employee.employee_id, None)
            return None
        task.metadata["travel_profile"] = asdict(profile)
        self.recorder.audit(
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
            self.recorder.audit(
                task, "BUDGET_SNAPSHOT_UNAVAILABLE", task.employee.employee_id, None
            )
            return None
        if snapshot is None:
            return None
        task.metadata["budget_snapshot"] = asdict(snapshot)
        self.recorder.audit(
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
        self.recorder.audit(task, "PROVENANCE_PINNED", option.option_id, digest)

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
