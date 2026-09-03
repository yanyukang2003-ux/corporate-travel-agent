"""审批与交接：分级审批、审批主体哈希、发件箱事件、交接单。

从 `TripWorkflowOrchestrator` 按职责拆出来的一块；方法原文逐字来自拆分前的 orchestrator.py。
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from corporate_travel_agent.agent.orchestrator.core import WorkflowError
from corporate_travel_agent.agent.orchestrator.state import OrchestratorState
from corporate_travel_agent.domain.enums import ApprovalStatus, TaskState
from corporate_travel_agent.domain.models import (
    ApprovalRequest,
    ApprovalStep,
    PolicyDecision,
    PolicySnapshot,
    TripTask,
)
from corporate_travel_agent.services.audit import stable_hash
from corporate_travel_agent.services.outbox_events import OutboxEventDraft


def _approval_subject_rules(decision: PolicyDecision) -> tuple[str, ...]:
    """这次审批到底在批哪几条规则。

    违规规则原样列出；系统判不了的加 `unjudged:` 前缀。两者都进审批主体哈希——
    "批的是一条违规"和"批的是一条查不到标准"不该哈希成同一件事。
    没有判不了的规则时，返回值和以前逐字相同，既有审批对象不受影响。
    """
    return (
        *decision.violation_ids,
        *(f"unjudged:{rule_id}" for rule_id in dict.fromkeys(decision.unjudged_rule_ids)),
    )


class ApprovalMixin(OrchestratorState):
    """审批与交接：分级审批、审批主体哈希、发件箱事件、交接单。

    混入 `TripWorkflowOrchestrator`；状态都在宿主实例上，这里只放方法。
    """

    def decide_approval(
        self,
        task_id: str,
        *,
        approver_id: str,
        approved: bool,
        reason: str,
    ) -> TripTask:
        """审批人对例外审批作出通过/驳回。"""
        task = self.tasks.get(task_id)
        approval = task.approval
        if task.state is not TaskState.WAITING_FOR_APPROVAL or approval is None:
            raise WorkflowError("The task has no pending approval")
        if approval.approver_id != approver_id:
            raise WorkflowError("The actor is not assigned to this approval")
        if approval.status is not ApprovalStatus.PENDING:
            raise WorkflowError("The approval has already been decided")
        if self.clock() >= approval.expires_at:
            approval.status = ApprovalStatus.INVALIDATED
            self._audit(
                task,
                "APPROVAL_INVALIDATED",
                approval.subject_hash,
                "expired",
                outbox=(self._approval_event(task, "APPROVAL_INVALIDATED"),),
            )
            task.selected_option_id = None
            task.failure = "The approval expired; select an option and request approval again"
            self._transition(task, TaskState.OPTIONS_READY)
            self._transition(task, TaskState.WAITING_FOR_USER)
            raise WorkflowError("The approval has expired")
        if approval.subject_hash != self._approval_subject_hash(task):
            approval.status = ApprovalStatus.INVALIDATED
            self._audit(
                task,
                "APPROVAL_INVALIDATED",
                approval.subject_hash,
                "subject_changed",
                outbox=(self._approval_event(task, "APPROVAL_INVALIDATED"),),
            )
            task.selected_option_id = None
            task.failure = "The approval subject changed; select an option again"
            self._transition(task, TaskState.OPTIONS_READY)
            self._transition(task, TaskState.WAITING_FOR_USER)
            raise WorkflowError("INV-004: approval subject has changed")

        decided_at = self.clock()
        step = approval.pending_step
        if step is not None:
            step.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
            step.decided_at = decided_at
            step.reason = reason
        if not approved:
            approval.status = ApprovalStatus.REJECTED
            approval.decision_reason = reason
            self._audit(
                task,
                "APPROVAL_DECIDED",
                approver_id,
                approval.status.value,
                outbox=(self._approval_event(task, "APPROVAL_DECIDED"),),
            )
            task.selected_option_id = None
            self._transition(task, TaskState.OPTIONS_READY)
            self._transition(task, TaskState.WAITING_FOR_USER)
            return task

        if step is not None and approval.remaining_steps > 0:
            # 这一级批了，还有下一级：审批单还挂着，只是换了个人。任务状态不动，
            # 投影里的"当前待谁批"跟着换，下一级的收件箱才看得见它。
            approval.current_step += 1
            approval.approver_id = approval.steps[approval.current_step].approver_id
            self._audit(
                task,
                "APPROVAL_STEP_ADVANCED",
                approver_id,
                {
                    "step_index": approval.current_step,
                    "next_approver_id": approval.approver_id,
                    "step_label": approval.steps[approval.current_step].label,
                },
                outbox=(self._approval_event(task, "APPROVAL_STEP_REQUESTED"),),
            )
            return task

        approval.status = ApprovalStatus.APPROVED
        approval.decision_reason = reason
        self._audit(
            task,
            "APPROVAL_DECIDED",
            approver_id,
            approval.status.value,
            outbox=(self._approval_event(task, "APPROVAL_DECIDED"),),
        )
        self._transition(task, TaskState.REVALIDATING)
        return self._revalidate_selected(task)

    def mark_handed_off(self, task_id: str) -> TripTask:
        """标记 Booking Intent 已交接给外部 Provider。"""
        task = self.tasks.get(task_id)
        if task.state is not TaskState.READY_FOR_HANDOFF or task.booking_intent is None:
            raise WorkflowError("No validated handoff is ready")
        self._transition(task, TaskState.HANDED_OFF)
        self._audit(task, "HANDOFF_COMPLETED", task.booking_intent.intent_id, task.state.value)
        return task

    def _new_approval(self, task: TripTask, business_reason: str) -> ApprovalRequest:
        """为需审批方案创建 ApprovalRequest。

        审批人要看的是**为什么轮到他来定**，而"违规了"和"系统判不了"是两个
        不同的理由。判不了的规则加 `unjudged:` 前缀写进同一串里：既不和真的
        违规规则混淆，也不用另开一个字段（那要动持久化、投影和 API）。
        """
        option = task.selected_option()
        if option is None:
            raise WorkflowError("No selected option")
        created_at = self.clock()
        violations = _approval_subject_rules(option.policy_decision)
        steps = self._approval_steps(task, self._policy_for(task), option.total_cost, violations)
        return ApprovalRequest(
            approval_id=str(uuid4()),
            subject_hash=self._approval_subject_hash(task),
            option_id=option.option_id,
            option_version=option.version,
            trip_request_version=self._request(task).version,
            policy_snapshot_id=task.policy_snapshot_id,
            employee_snapshot_id=task.employee.snapshot_id,
            violations=violations,
            business_reason=business_reason,
            approver_id=steps[0].approver_id,
            approved_price=option.total_cost,
            created_at=created_at,
            expires_at=created_at + timedelta(hours=24),
            steps=steps,
            current_step=0,
        )

    @staticmethod
    def _approval_steps(
        task: TripTask,
        policy: PolicySnapshot,
        price: Decimal,
        violations: tuple[str, ...],
    ) -> tuple[ApprovalStep, ...]:
        """审批链：直属经理永远是第一级，之后按政策的分级条件追加。

        触发条件看两样：方案总价过了某一档，或者违规/判不了的规则里有某一条
        （"判不了"的规则带 `unjudged:` 前缀，去掉前缀再比）。同一个人不会出现两次。
        """
        rule_ids = {item.removeprefix("unjudged:") for item in violations}
        steps = [ApprovalStep(approver_id=task.employee.manager_id, label="manager")]
        for tier in policy.approval_tiers:
            if not tier.applies(price, rule_ids):
                continue
            if any(step.approver_id == tier.approver_id for step in steps):
                continue
            steps.append(ApprovalStep(approver_id=tier.approver_id, label=tier.label))
        return tuple(steps)

    def _approval_event(self, task: TripTask, event_type: str) -> OutboxEventDraft:
        """给外部审批系统（或收件箱）的通知：只放 ID、金额、规则和申请理由，不放正文原文。"""
        approval = task.approval
        assert approval is not None
        option = task.selected_option()
        step = approval.pending_step
        return OutboxEventDraft(
            event_type=event_type,
            payload={
                "task_id": task.task_id,
                "approval_id": approval.approval_id,
                "status": approval.status.value,
                "approver_id": approval.approver_id,
                "step_index": approval.current_step,
                "step_label": step.label if step is not None else "manager",
                "step_count": len(approval.steps) or 1,
                "employee_id": task.employee.employee_id,
                "requester_id": task.requested_by,
                "approved_price": str(approval.approved_price),
                "currency": option.currency if option is not None else None,
                "violations": list(approval.violations),
                "business_reason": approval.business_reason,
                "decision_reason": approval.decision_reason,
                "expires_at": approval.expires_at.isoformat(),
            },
        )

    def _approval_subject_hash(self, task: TripTask) -> str:
        option = task.selected_option()
        if option is None:
            raise WorkflowError("No selected option")
        return stable_hash(
            {
                "trip_request_version": self._request(task).version,
                "employee_snapshot_id": task.employee.snapshot_id,
                "policy_snapshot_id": task.policy_snapshot_id,
                "option_id": option.option_id,
                "option_version": option.version,
                "approved_price": str(option.total_cost),
                "violations": _approval_subject_rules(option.policy_decision),
            }
        )
