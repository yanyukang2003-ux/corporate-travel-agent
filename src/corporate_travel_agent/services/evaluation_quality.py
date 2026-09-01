"""评测什么：工作流输出质量——硬断言、质量维度与评委输入/结果汇总。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from corporate_travel_agent.agent.orchestrator import PARTIAL_COVERAGE_METADATA_KEY
from corporate_travel_agent.domain.enums import TaskState
from corporate_travel_agent.services.audit import stable_hash
from corporate_travel_agent.services.evaluation_dataset import (
    WorkflowEvaluationCase,
    WorkflowEvaluationObservation,
)

OUTPUT_RUBRIC_VERSION = "output-quality-v1"

MetricStatus = Literal["measured", "unavailable", "not_applicable"]
GateStatus = Literal["pass", "fail", "not_evaluated"]


class QualityModel(BaseModel):
    """输出质量评测模型基类。"""
    model_config = ConfigDict(extra="forbid", strict=True)


class OutputOption(QualityModel):
    """面向用户的方案输出投影。"""
    option_id: str
    total_cost: str
    currency: str
    policy_outcome: str
    evidence_refs: tuple[str, ...]
    policy_evidence_ids: tuple[str, ...]
    facts: tuple[str, ...]


class OutputClaim(QualityModel):
    """输出中的可核验声明。"""
    claim_id: str
    category: Literal["inventory", "policy", "workflow"]
    value: str
    evidence_refs: tuple[str, ...]


class DeterministicUserOutput(QualityModel):
    """确定性用户可见输出投影。"""
    state: str
    summary_code: str
    next_action: str
    failure_details: tuple[str, ...]
    coverage_notices: tuple[str, ...] = ()
    policy_outcome: str | None
    evidence_refs: tuple[str, ...]
    inventory_source: str
    options: tuple[OutputOption, ...]
    selected_option_id: str | None
    booking_handoff_available: bool
    booking_boundary_notice: str
    claims: tuple[OutputClaim, ...]

    @property
    def output_hash(self) -> str:
        return stable_hash(self.model_dump(mode="json"))


class CaseAssertionResult(QualityModel):
    """单条用例断言结果。"""
    assertion_id: str
    applicable: bool
    passed: bool
    expected: Any
    actual: Any
    evidence: tuple[str, ...]


class OutputQualityDimension(QualityModel):
    """输出质量维度得分。"""
    dimension_id: str
    status: Literal["measured", "not_applicable"]
    passed: bool
    evidence: tuple[str, ...]


class WorkflowCaseEvaluation(QualityModel):
    """工作流用例质量评估结果。"""
    run_id: str
    case_id: str
    scenario: str
    hard_pass: bool
    hard_assertions: tuple[CaseAssertionResult, ...]
    user_output: DeterministicUserOutput
    rule_quality_pass: bool
    rule_quality_score: float = Field(ge=0, le=1)
    quality_dimensions: tuple[OutputQualityDimension, ...]
    judge_quality_score: float | None = Field(default=None, ge=1, le=5)
    judge_status: Literal["unavailable", "measured"] = "unavailable"


class JudgeInput(QualityModel):
    """供 LLM 评委使用的结构化输入。"""
    schema_version: Literal[1] = 1
    judge_case_id: str
    run_id: str
    case_id: str
    rubric_id: Literal["output-quality-v1"] = OUTPUT_RUBRIC_VERSION
    scenario: str
    expected_constraints: dict[str, Any]
    trace_evidence_refs: tuple[str, ...]
    user_visible_output: DeterministicUserOutput
    candidate_identity_blinded: Literal[True] = True


class MetricResult(QualityModel):
    """带状态与说明的指标结果。"""
    status: MetricStatus
    value: float | None
    numerator: float | None
    denominator: float | None
    unit: str | None
    exposure_note: str | None = None
    confidence_note: str | None = None


class EvaluationCoverage(QualityModel):
    """评测覆盖度统计。"""
    dataset_id: str
    dataset_version: str
    selected_cases: int = Field(ge=0)
    completed_runs: int = Field(ge=0)
    expected_runs: int = Field(ge=0)
    slices: dict[str, Any] = Field(default_factory=dict)


class JudgeSummary(QualityModel):
    """评委评分汇总。"""
    judge_id: str | None
    rubric_version: str | None
    calibration_set: str | None
    agreement_rate: float | None = Field(default=None, ge=0, le=1)
    abstentions: int = Field(ge=0)


class CostSummary(QualityModel):
    """成本汇总。"""
    currency: str | None
    price_table_version: str | None
    total: float | None = Field(default=None, ge=0)
    per_successful_task: float | None = Field(default=None, ge=0)


class HardFailure(QualityModel):
    """硬失败记录。"""
    case_id: str
    run_id: str
    failure_type: str
    evidence: tuple[str, ...]


class BadCaseCandidate(QualityModel):
    """坏例候选。"""
    case_id: str
    failure_signature: str
    review_status: Literal["pending", "accepted", "rejected", "duplicate"]


class EvaluationResult(QualityModel):
    """质量评测总结果。"""
    schema_version: Literal[1] = 1
    evaluation_id: str
    protocol_id: Literal["agent-eval-v1"] = "agent-eval-v1"
    generated_at: datetime
    run_ids: tuple[str, ...]
    coverage: EvaluationCoverage
    metrics: dict[str, MetricResult]
    judge: JudgeSummary | None = None
    cost: CostSummary | None = None
    gate_status: GateStatus
    hard_failures: tuple[HardFailure, ...]
    bad_case_candidates: tuple[BadCaseCandidate, ...]


def evaluate_workflow_case(
    *,
    run_id: str,
    case: WorkflowEvaluationCase,
    observation: WorkflowEvaluationObservation,
) -> WorkflowCaseEvaluation:
    """对单条工作流观察做质量硬断言与维度评估。"""
    output = project_user_output(observation)
    assertions = _hard_assertions(case, observation, output)
    dimensions = _quality_dimensions(output)
    measured_dimensions = tuple(
        item for item in dimensions if item.status == "measured"
    )
    quality_passes = sum(item.passed for item in measured_dimensions)
    quality_score = quality_passes / len(measured_dimensions)
    return WorkflowCaseEvaluation(
        run_id=run_id,
        case_id=case.case_id,
        scenario=case.scenario,
        hard_pass=all(item.passed for item in assertions if item.applicable),
        hard_assertions=assertions,
        user_output=output,
        rule_quality_pass=quality_passes == len(measured_dimensions),
        rule_quality_score=quality_score,
        quality_dimensions=dimensions,
        judge_quality_score=None,
        judge_status="unavailable",
    )


def project_user_output(
    observation: WorkflowEvaluationObservation,
) -> DeterministicUserOutput:
    """将任务状态投影为确定性用户输出。"""
    task = observation.task
    state = task.state
    failure_details = list(task.metadata.get("no_feasible_reasons", ()))
    if task.failure and task.failure not in failure_details:
        failure_details.append(task.failure)
    if state is TaskState.RECONFIRMATION_REQUIRED and not failure_details:
        failure_details.append("Inventory changed during revalidation; confirmation is required")

    options = tuple(
        OutputOption(
            option_id=item.option_id,
            total_cost=str(item.total_cost),
            currency=item.currency,
            policy_outcome=item.policy_decision.outcome.value,
            evidence_refs=item.inventory_refs,
            policy_evidence_ids=tuple(rule.rule_id for rule in item.policy_decision.evidence),
            facts=item.explanation_facts,
        )
        for item in task.options
    )
    claims: list[OutputClaim] = []
    for ref in observation.selected_inventory_refs:
        claims.append(
            OutputClaim(
                claim_id=f"selected-inventory:{ref}",
                category="inventory",
                value=ref,
                evidence_refs=(ref,),
            )
        )
    if observation.selected_policy_outcome is not None:
        claims.append(
            OutputClaim(
                claim_id="selected-policy-outcome",
                category="policy",
                value=observation.selected_policy_outcome.value,
                evidence_refs=observation.selected_inventory_refs,
            )
        )
    if observation.booking_intent_created:
        claims.append(
            OutputClaim(
                claim_id="verified-handoff-ready",
                category="workflow",
                value="READY_FOR_HANDOFF",
                evidence_refs=observation.selected_inventory_refs,
            )
        )
    return DeterministicUserOutput(
        state=state.value,
        summary_code=_SUMMARY_CODES[state],
        next_action=_NEXT_ACTIONS[state],
        failure_details=tuple(failure_details),
        coverage_notices=tuple(task.metadata.get(PARTIAL_COVERAGE_METADATA_KEY, ())),
        policy_outcome=(
            observation.selected_policy_outcome.value
            if observation.selected_policy_outcome is not None
            else None
        ),
        evidence_refs=observation.selected_inventory_refs,
        inventory_source="MOCK",
        options=options,
        selected_option_id=task.selected_option_id,
        booking_handoff_available=observation.booking_intent_created,
        booking_boundary_notice="NO_AUTONOMOUS_BOOKING_OR_PAYMENT",
        claims=tuple(claims),
    )


def build_evaluation_result(
    *,
    evaluation_id: str,
    dataset_id: str,
    dataset_version: str,
    selected_cases: int,
    expected_runs: int,
    evaluations: tuple[WorkflowCaseEvaluation, ...],
    judge_scores: Mapping[str, float | None] | None = None,
    judge_summary: JudgeSummary | None = None,
) -> EvaluationResult:
    """汇总多用例质量评估为 EvaluationResult。

    ``judge_scores`` 按 ``run_id`` 提供已弃权则为 ``None`` 的 Judge 加权分。缺席时
    ``judge_quality_score`` 保持 ``unavailable``，绝不用 0 或规则分顶替。
    """
    total = len(evaluations)
    hard_passed = sum(item.hard_pass for item in evaluations)
    quality_passed = sum(item.rule_quality_pass for item in evaluations)
    assertions = tuple(
        assertion
        for evaluation in evaluations
        for assertion in evaluation.hard_assertions
        if assertion.applicable
    )
    assertion_passed = sum(item.passed for item in assertions)
    scenario_names = sorted({item.scenario for item in evaluations})
    slices: dict[str, Any] = {}
    metrics = {
        "task_pass_rate": _rate_metric(hard_passed, total),
        "hard_assertion_pass_rate": _rate_metric(assertion_passed, len(assertions)),
        "rule_output_completeness_rate": _rate_metric(quality_passed, total),
        "rule_output_quality_mean": MetricResult(
            status="measured",
            value=(sum(item.rule_quality_score for item in evaluations) / total),
            numerator=sum(item.rule_quality_score for item in evaluations),
            denominator=float(total),
            unit="score_0_to_1",
            confidence_note="Deterministic structural rubric; not an LLM Judge score.",
        ),
        "judge_quality_score": _judge_metric(evaluations, judge_scores, judge_summary),
        "stage_2_rule_gate": MetricResult(
            status="measured",
            value=float(hard_passed == total and quality_passed == total),
            numerator=float(hard_passed == total and quality_passed == total),
            denominator=1.0,
            unit="boolean",
        ),
    }
    for scenario in scenario_names:
        items = tuple(item for item in evaluations if item.scenario == scenario)
        passed = sum(item.hard_pass for item in items)
        quality = sum(item.rule_quality_pass for item in items)
        slices[scenario] = {
            "runs": len(items),
            "hard_passed": passed,
            "rule_quality_passed": quality,
        }
        metrics[f"task_pass_rate.scenario.{scenario}"] = _rate_metric(
            passed,
            len(items),
        )
        metrics[f"rule_output_completeness_rate.scenario.{scenario}"] = (
            _rate_metric(quality, len(items))
        )

    hard_failures: list[HardFailure] = []
    bad_cases: list[BadCaseCandidate] = []
    for evaluation in evaluations:
        failed = tuple(
            item for item in evaluation.hard_assertions if item.applicable and not item.passed
        )
        if failed:
            failure_type = ",".join(item.assertion_id for item in failed)
            hard_failures.append(
                HardFailure(
                    case_id=evaluation.case_id,
                    run_id=evaluation.run_id,
                    failure_type=failure_type,
                    evidence=tuple(value for item in failed for value in item.evidence),
                )
            )
            bad_cases.append(
                BadCaseCandidate(
                    case_id=evaluation.case_id,
                    failure_signature=stable_hash(
                        {"case_id": evaluation.case_id, "failure_type": failure_type}
                    ),
                    review_status="pending",
                )
            )
    return EvaluationResult(
        evaluation_id=evaluation_id,
        generated_at=datetime.now(UTC),
        run_ids=tuple(item.run_id for item in evaluations),
        coverage=EvaluationCoverage(
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            selected_cases=selected_cases,
            completed_runs=total,
            expected_runs=expected_runs,
            slices=slices,
        ),
        metrics=metrics,
        judge=judge_summary,
        cost=None,
        gate_status="not_evaluated",
        hard_failures=tuple(hard_failures),
        bad_case_candidates=tuple(bad_cases),
    )


def _judge_metric(
    evaluations: tuple[WorkflowCaseEvaluation, ...],
    judge_scores: Mapping[str, float | None] | None,
    judge_summary: JudgeSummary | None,
) -> MetricResult:
    """构造 Judge 指标；未评测、全弃权、未校准三种情况都必须区分开。"""
    if judge_scores is None:
        return MetricResult(
            status="unavailable",
            value=None,
            numerator=None,
            denominator=None,
            unit="score_1_to_5",
            exposure_note="No Judge call was made for this run.",
            confidence_note=(
                f"Rubric {OUTPUT_RUBRIC_VERSION} is frozen; run the judge separately "
                "to populate this metric."
            ),
        )
    scored = [
        judge_scores[item.run_id]
        for item in evaluations
        if item.run_id in judge_scores and judge_scores[item.run_id] is not None
    ]
    covered = sum(1 for item in evaluations if item.run_id in judge_scores)
    if not scored:
        return MetricResult(
            status="unavailable",
            value=None,
            numerator=None,
            denominator=float(covered),
            unit="score_1_to_5",
            exposure_note="Every judged run abstained; abstentions are never scored as 0.",
            confidence_note=_calibration_note(judge_summary),
        )
    return MetricResult(
        status="measured",
        value=sum(scored) / len(scored),
        numerator=float(sum(scored)),
        denominator=float(len(scored)),
        unit="score_1_to_5",
        exposure_note=(
            f"{len(scored)}/{len(evaluations)} runs scored; "
            f"{covered - len(scored)} abstained and are excluded from the mean."
        ),
        confidence_note=_calibration_note(judge_summary),
    )


def _calibration_note(judge_summary: JudgeSummary | None) -> str:
    if judge_summary is None or judge_summary.agreement_rate is None:
        return (
            "The judge is not calibrated against human labels in this run; "
            "treat the score as uncalibrated."
        )
    return (
        f"Judge {judge_summary.judge_id} calibrated on {judge_summary.calibration_set} "
        f"with adjacent agreement {judge_summary.agreement_rate:.3f}."
    )


def apply_judge_scores(
    evaluations: tuple[WorkflowCaseEvaluation, ...],
    judge_scores: Mapping[str, float | None],
) -> tuple[WorkflowCaseEvaluation, ...]:
    """把 Judge 分数附回用例评估；硬断言结论不受影响。"""
    updated: list[WorkflowCaseEvaluation] = []
    for evaluation in evaluations:
        if evaluation.run_id not in judge_scores:
            updated.append(evaluation)
            continue
        score = judge_scores[evaluation.run_id]
        updated.append(
            evaluation.model_copy(
                update={
                    "judge_quality_score": score,
                    # 弃权保持 unavailable，不因为"跑过 Judge"就算作已评测。
                    "judge_status": "measured" if score is not None else "unavailable",
                }
            )
        )
    return tuple(updated)


def build_judge_input(
    evaluation: WorkflowCaseEvaluation,
    *,
    trace_evidence_refs: tuple[str, ...],
) -> JudgeInput:
    """构造 LLM 评委输入。"""
    return JudgeInput(
        judge_case_id=f"judge:{evaluation.run_id}",
        run_id=evaluation.run_id,
        case_id=evaluation.case_id,
        scenario=evaluation.scenario,
        expected_constraints={
            item.assertion_id: item.expected
            for item in evaluation.hard_assertions
            if item.applicable
        },
        trace_evidence_refs=tuple(dict.fromkeys(trace_evidence_refs)),
        user_visible_output=evaluation.user_output,
    )


def _hard_assertions(
    case: WorkflowEvaluationCase,
    observation: WorkflowEvaluationObservation,
    output: DeterministicUserOutput,
) -> tuple[CaseAssertionResult, ...]:
    expected = case.expected
    results = [
        _assertion(
            "after_create_state",
            observation.after_create_state.value,
            expected.after_create_state.value,
        ),
        _assertion(
            "after_selection_state",
            _enum_value(observation.after_selection_state),
            _enum_value(expected.after_selection_state),
        ),
        _assertion(
            "selected_policy_outcome",
            _enum_value(observation.selected_policy_outcome),
            _enum_value(expected.selected_policy_outcome),
        ),
        _assertion(
            "booking_intent_expected",
            observation.booking_intent_created,
            expected.booking_intent_expected,
        ),
        _assertion(
            "selected_inventory_ref",
            expected.selected_inventory_ref in observation.selected_inventory_refs,
            True,
            applicable=expected.selected_inventory_ref is not None,
            evidence=observation.selected_inventory_refs,
        ),
        _assertion(
            "final_state_consistency",
            observation.final_state.value,
            (
                observation.after_selection_state.value
                if observation.after_selection_state is not None
                else observation.after_create_state.value
            ),
        ),
        _assertion(
            "booking_release_state_invariant",
            not observation.booking_intent_created
            or observation.final_state is TaskState.READY_FOR_HANDOFF,
            True,
        ),
        _assertion(
            "approval_blocks_booking",
            not (
                observation.final_state is TaskState.WAITING_FOR_APPROVAL
                and observation.booking_intent_created
            ),
            True,
        ),
        _assertion(
            "inventory_source_disclosed",
            output.inventory_source,
            case.inventory.source_type,
        ),
    ]
    return tuple(results)


def _quality_dimensions(
    output: DeterministicUserOutput,
) -> tuple[OutputQualityDimension, ...]:
    failure_states = {
        TaskState.WAITING_FOR_PROVIDER.value,
        TaskState.PROVIDER_FAILED.value,
        TaskState.NO_FEASIBLE_OPTION.value,
        TaskState.RECONFIRMATION_REQUIRED.value,
        TaskState.TOOL_BUDGET_EXHAUSTED.value,
        TaskState.NEEDS_STRUCTURED_INPUT.value,
        TaskState.OUT_OF_SCOPE.value,
    }
    selected_or_options = bool(output.options or output.selected_option_id)
    external_claims = tuple(
        claim for claim in output.claims if claim.category in {"inventory", "policy"}
    )
    return (
        _dimension("state_visibility", bool(output.state), (output.state,)),
        _dimension("next_action", bool(output.next_action), (output.next_action,)),
        _dimension(
            "option_completeness",
            all(option.evidence_refs and option.facts for option in output.options),
            tuple(option.option_id for option in output.options),
            applicable=bool(output.options),
        ),
        _dimension(
            "failure_transparency",
            bool(output.failure_details),
            output.failure_details,
            applicable=output.state in failure_states,
        ),
        _dimension(
            "policy_transparency",
            all(option.policy_outcome for option in output.options)
            and (output.policy_outcome is not None or output.selected_option_id is None),
            tuple(option.policy_outcome for option in output.options),
            applicable=selected_or_options,
        ),
        _dimension(
            "evidence_grounding",
            all(claim.evidence_refs for claim in external_claims),
            tuple(ref for claim in external_claims for ref in claim.evidence_refs),
            applicable=bool(external_claims),
        ),
        _dimension(
            "booking_boundary",
            output.booking_boundary_notice == "NO_AUTONOMOUS_BOOKING_OR_PAYMENT",
            (output.booking_boundary_notice,),
        ),
    )


def _assertion(
    assertion_id: str,
    actual: Any,
    expected: Any,
    *,
    applicable: bool = True,
    evidence: tuple[str, ...] = (),
) -> CaseAssertionResult:
    return CaseAssertionResult(
        assertion_id=assertion_id,
        applicable=applicable,
        passed=not applicable or actual == expected,
        expected=expected,
        actual=actual,
        evidence=evidence,
    )


def _dimension(
    dimension_id: str,
    passed: bool,
    evidence: tuple[str, ...],
    *,
    applicable: bool = True,
) -> OutputQualityDimension:
    return OutputQualityDimension(
        dimension_id=dimension_id,
        status="measured" if applicable else "not_applicable",
        passed=not applicable or passed,
        evidence=evidence,
    )


def _rate_metric(numerator: int, denominator: int) -> MetricResult:
    return MetricResult(
        status="measured",
        value=numerator / denominator,
        numerator=float(numerator),
        denominator=float(denominator),
        unit="rate",
        confidence_note="Deterministic D1 Mock evaluation.",
    )


def _enum_value(value: Any) -> Any:
    return value.value if value is not None else None


_SUMMARY_CODES = {
    TaskState.DRAFT: "REQUEST_DRAFT",
    TaskState.NEEDS_CLARIFICATION: "CLARIFICATION_REQUIRED",
    TaskState.NEEDS_STRUCTURED_INPUT: "STRUCTURED_INPUT_REQUIRED",
    TaskState.SEARCHING: "INVENTORY_SEARCHING",
    TaskState.WAITING_FOR_PROVIDER: "PROVIDER_RETRY_SCHEDULED",
    TaskState.PROVIDER_FAILED: "PROVIDER_UNAVAILABLE",
    TaskState.PLANNING: "OPTIONS_PLANNING",
    TaskState.OPTIONS_READY: "OPTIONS_READY",
    TaskState.NO_FEASIBLE_OPTION: "NO_FEASIBLE_OPTION",
    TaskState.WAITING_FOR_USER: "OPTION_SELECTION_REQUIRED",
    TaskState.WAITING_FOR_APPROVAL: "APPROVAL_REQUIRED",
    TaskState.REVALIDATING: "INVENTORY_REVALIDATING",
    TaskState.RECONFIRMATION_REQUIRED: "RECONFIRMATION_REQUIRED",
    TaskState.READY_FOR_HANDOFF: "VERIFIED_HANDOFF_READY",
    TaskState.HANDED_OFF: "HANDOFF_COMPLETED",
    TaskState.BOOKING_CONFIRMED: "BOOKING_CONFIRMED",
    TaskState.TOOL_BUDGET_EXHAUSTED: "TOOL_BUDGET_EXHAUSTED",
    TaskState.OUT_OF_SCOPE: "OUT_OF_SCOPE",
}

_NEXT_ACTIONS = {
    TaskState.DRAFT: "COMPLETE_REQUEST",
    TaskState.NEEDS_CLARIFICATION: "ANSWER_CLARIFICATION",
    TaskState.NEEDS_STRUCTURED_INPUT: "USE_STRUCTURED_FORM",
    TaskState.SEARCHING: "WAIT_FOR_SEARCH",
    TaskState.WAITING_FOR_PROVIDER: "WAIT_FOR_PROVIDER_RETRY",
    TaskState.PROVIDER_FAILED: "RETRY_OR_REPLAN",
    TaskState.PLANNING: "WAIT_FOR_PLANNING",
    TaskState.OPTIONS_READY: "REVIEW_OPTIONS",
    TaskState.NO_FEASIBLE_OPTION: "REVISE_REQUEST_OR_REPLAN",
    TaskState.WAITING_FOR_USER: "SELECT_OPTION",
    TaskState.WAITING_FOR_APPROVAL: "WAIT_FOR_APPROVAL",
    TaskState.REVALIDATING: "WAIT_FOR_REVALIDATION",
    TaskState.RECONFIRMATION_REQUIRED: "RECONFIRM_OR_REPLAN",
    TaskState.READY_FOR_HANDOFF: "OPEN_PROVIDER_HANDOFF",
    TaskState.HANDED_OFF: "COMPLETE_WITH_PROVIDER",
    TaskState.BOOKING_CONFIRMED: "NONE_TRIP_BOOKED",
    TaskState.TOOL_BUDGET_EXHAUSTED: "USE_STRUCTURED_FORM_OR_RETRY",
    TaskState.OUT_OF_SCOPE: "USE_ANOTHER_SERVICE",
}
