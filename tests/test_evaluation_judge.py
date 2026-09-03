"""LLM Judge 的契约、硬规则优先、弃权语义与人工校准。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corporate_travel_agent.agent.ports import LLMCallMetadata
from corporate_travel_agent.evaluation.judge import (
    JudgeDimensionScore,
    JudgeError,
    JudgeScore,
    assert_blinded,
    calibrate_against_human,
    load_human_output_quality_annotations,
    load_judge_inputs,
    load_output_quality_rubric,
    mean_judge_score,
    score_judge_inputs,
    summarize_judge_verdicts,
)
from corporate_travel_agent.evaluation.quality import (
    apply_judge_scores,
)

REPO = Path(__file__).resolve().parents[1]
RUBRIC_PATH = REPO / "evals/rubrics/output-quality-v1.json"
JUDGE_INPUTS = REPO / "reports/evaluation-runs/phase2-quality-20260802/judge-inputs.jsonl"
ANNOTATIONS = (
    REPO / "data/evaluation/human-annotations/v1/to-label/03-output-quality.jsonl"
)


class ScriptedJudge:
    """按固定分数或弃权回应的离线 Judge。"""

    judge_id = "scripted-judge-v1"
    judge_prompt_version = "scripted-judge-prompt-v1"

    def __init__(self, *, score: int = 4, abstain: bool = False, per_run=None) -> None:
        self.default_score = score
        self.abstain = abstain
        self.per_run = per_run or {}
        self.calls = 0

    def score(self, judge_input, rubric):  # noqa: A003 - port method name
        self.calls += 1
        metadata = LLMCallMetadata(
            prompt_version=self.judge_prompt_version,
            model="scripted-judge",
            duration_ms=1,
        )
        if self.abstain:
            return (
                JudgeScore(
                    abstained=True,
                    abstain_reason="Required trace evidence is unavailable.",
                    dimensions=[],
                ),
                metadata,
            )
        value = self.per_run.get(judge_input.run_id, self.default_score)
        return (
            JudgeScore(
                abstained=False,
                abstain_reason=None,
                dimensions=[
                    JudgeDimensionScore(
                        dimension_id=item.id,
                        score=value,
                        rationale="scripted",
                    )
                    for item in rubric.dimensions
                ],
            ),
            metadata,
        )


@pytest.fixture(name="rubric")
def rubric_fixture():
    return load_output_quality_rubric(RUBRIC_PATH)


@pytest.fixture(name="inputs")
def inputs_fixture():
    return load_judge_inputs(JUDGE_INPUTS)


def test_rubric_weights_must_sum_to_one(rubric) -> None:
    assert abs(sum(item.weight for item in rubric.dimensions) - 1.0) < 1e-9
    assert rubric.hard_rule_precedence is True
    assert rubric.content_sha256


def test_rubric_with_unbalanced_weights_is_rejected(tmp_path: Path, rubric) -> None:
    payload = json.loads(RUBRIC_PATH.read_text(encoding="utf-8"))
    payload["dimensions"][0]["weight"] = 0.9
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(JudgeError, match="weights must sum"):
        load_output_quality_rubric(broken)


def test_judge_inputs_are_blinded(rubric, inputs) -> None:
    for judge_input in inputs:
        assert_blinded(judge_input, rubric)


def test_unblinded_input_is_rejected(rubric, inputs) -> None:
    leaked = inputs[0].model_copy(update={"candidate_identity_blinded": False})
    with pytest.raises(JudgeError, match="not blinded"):
        assert_blinded(leaked, rubric)


def test_scoring_produces_a_weighted_score_in_range(rubric, inputs) -> None:
    verdicts, metadata = score_judge_inputs(
        inputs[:3], judge=ScriptedJudge(score=4), rubric=rubric
    )
    assert len(verdicts) == 3
    assert len(metadata) == 3
    for verdict in verdicts:
        assert verdict.weighted_score == pytest.approx(4.0)
        assert verdict.rubric_sha256 == rubric.content_sha256
        assert not verdict.abstained


def test_abstention_is_never_scored_as_zero(rubric, inputs) -> None:
    verdicts, _ = score_judge_inputs(
        inputs[:3], judge=ScriptedJudge(abstain=True), rubric=rubric
    )
    assert all(item.weighted_score is None for item in verdicts)
    assert mean_judge_score(verdicts) is None
    summary = summarize_judge_verdicts(verdicts, rubric=rubric)
    assert summary.abstentions == 3
    assert summary.agreement_rate is None


def test_hard_rule_precedence_is_recorded_not_overridden(rubric, inputs) -> None:
    failing = inputs[0].case_id
    verdicts, _ = score_judge_inputs(
        inputs[:2],
        judge=ScriptedJudge(score=5),
        rubric=rubric,
        hard_failed_case_ids=frozenset({failing}),
    )
    flagged = [item for item in verdicts if item.hard_rule_failed]
    assert len(flagged) == 1
    # 高分不会把硬失败洗白：标记必须与分数共存。
    assert flagged[0].weighted_score == pytest.approx(5.0)


def test_judge_must_score_every_dimension(rubric, inputs) -> None:
    class PartialJudge(ScriptedJudge):
        def score(self, judge_input, rubric):  # noqa: A003
            metadata = LLMCallMetadata(
                prompt_version=self.judge_prompt_version,
                model="scripted-judge",
                duration_ms=1,
            )
            return (
                JudgeScore(
                    abstained=False,
                    abstain_reason=None,
                    dimensions=[
                        JudgeDimensionScore(
                            dimension_id=rubric.dimensions[0].id,
                            score=4,
                            rationale="partial",
                        )
                    ],
                ),
                metadata,
            )

    with pytest.raises(JudgeError, match="omitted dimensions"):
        score_judge_inputs(inputs[:1], judge=PartialJudge(), rubric=rubric)


def test_judge_cannot_invent_a_dimension(rubric, inputs) -> None:
    class ExtraJudge(ScriptedJudge):
        def score(self, judge_input, rubric):  # noqa: A003
            metadata = LLMCallMetadata(
                prompt_version=self.judge_prompt_version,
                model="scripted-judge",
                duration_ms=1,
            )
            dimensions = [
                JudgeDimensionScore(dimension_id=item.id, score=4, rationale="ok")
                for item in rubric.dimensions
            ]
            dimensions.append(
                JudgeDimensionScore(
                    dimension_id="creativity", score=5, rationale="invented"
                )
            )
            return (
                JudgeScore(abstained=False, abstain_reason=None, dimensions=dimensions),
                metadata,
            )

    with pytest.raises(JudgeError, match="unknown dimensions"):
        score_judge_inputs(inputs[:1], judge=ExtraJudge(), rubric=rubric)


def test_abstention_without_a_reason_is_rejected(rubric, inputs) -> None:
    class SilentJudge(ScriptedJudge):
        def score(self, judge_input, rubric):  # noqa: A003
            metadata = LLMCallMetadata(
                prompt_version=self.judge_prompt_version,
                model="scripted-judge",
                duration_ms=1,
            )
            return (
                JudgeScore(abstained=True, abstain_reason=None, dimensions=[]),
                metadata,
            )

    with pytest.raises(JudgeError, match="without a reason"):
        score_judge_inputs(inputs[:1], judge=SilentJudge(), rubric=rubric)


def test_verdicts_from_different_judges_are_not_merged(rubric, inputs) -> None:
    first, _ = score_judge_inputs(inputs[:1], judge=ScriptedJudge(), rubric=rubric)
    second_judge = ScriptedJudge()
    second_judge.judge_id = "other-judge"
    second, _ = score_judge_inputs(inputs[1:2], judge=second_judge, rubric=rubric)
    with pytest.raises(JudgeError, match="different judges"):
        summarize_judge_verdicts((*first, *second), rubric=rubric)


def test_calibration_reports_agreement_against_human_labels(rubric, inputs) -> None:
    annotations, sha = load_human_output_quality_annotations(ANNOTATIONS)
    human_scores = {
        record["input"]["run_id"]: record["annotation"]["ratings"]["completeness"]
        for record in annotations
    }
    verdicts, _ = score_judge_inputs(
        inputs,
        judge=ScriptedJudge(per_run=human_scores),
        rubric=rubric,
    )
    report = calibrate_against_human(
        verdicts,
        rubric=rubric,
        annotations=annotations,
        annotation_file="03-output-quality.jsonl",
        annotation_sha256=sha,
        judge_id="scripted-judge-v1",
    )
    assert report.compared_cases == 20
    assert report.adjacent_agreement_rate is not None
    assert report.adjacent_agreement_rate >= 0.9
    # 单标注者单轮不构成人工双评，因此校准状态必须是样本不足而不是通过。
    assert report.human_double_rated is False
    assert report.calibration_status == "insufficient_samples"
    assert report.annotator_ids == ("human-01",)


def test_calibration_flags_major_disagreement(rubric, inputs) -> None:
    annotations, sha = load_human_output_quality_annotations(ANNOTATIONS)
    verdicts, _ = score_judge_inputs(
        inputs, judge=ScriptedJudge(score=1), rubric=rubric
    )
    report = calibrate_against_human(
        verdicts,
        rubric=rubric,
        annotations=annotations,
        annotation_file="03-output-quality.jsonl",
        annotation_sha256=sha,
        judge_id="scripted-judge-v1",
    )
    assert report.major_disagreements
    assert report.adjacent_agreement_rate is not None
    assert report.adjacent_agreement_rate < 0.9


def test_apply_judge_scores_keeps_abstentions_unavailable() -> None:
    from corporate_travel_agent.evaluation.quality import (
        CaseAssertionResult,
        DeterministicUserOutput,
        WorkflowCaseEvaluation,
    )

    output = DeterministicUserOutput(
        state="READY_FOR_HANDOFF",
        summary_code="VERIFIED_HANDOFF_READY",
        next_action="OPEN_PROVIDER_HANDOFF",
        failure_details=(),
        policy_outcome="COMPLIANT",
        evidence_refs=("REF-1",),
        inventory_source="MOCK",
        options=(),
        selected_option_id=None,
        booking_handoff_available=True,
        booking_boundary_notice="NO_AUTONOMOUS_BOOKING_OR_PAYMENT",
        claims=(),
    )
    evaluation = WorkflowCaseEvaluation(
        run_id="run-1",
        case_id="case-1",
        scenario="COMPLIANT",
        hard_pass=True,
        hard_assertions=(
            CaseAssertionResult(
                assertion_id="after_create_state",
                applicable=True,
                passed=True,
                expected="WAITING_FOR_USER",
                actual="WAITING_FOR_USER",
                evidence=(),
            ),
        ),
        user_output=output,
        rule_quality_pass=True,
        rule_quality_score=1.0,
        quality_dimensions=(),
    )
    updated = apply_judge_scores((evaluation,), {"run-1": None})
    assert updated[0].judge_status == "unavailable"
    scored = apply_judge_scores((evaluation,), {"run-1": 4.25})
    assert scored[0].judge_status == "measured"
    assert scored[0].judge_quality_score == pytest.approx(4.25)


def test_single_annotator_mode_is_opt_in_and_never_reports_plain_passed(
    rubric, inputs
) -> None:
    """放行单标注者后要给出结论，但结论名里必须带着依据。

    默认仍然是 insufficient_samples：一个人打的分证明不了分数客观，只能证明评委
    和这个人想的差不多。项目所有者可以显式放行这道门，但报告里不能伪装成已校准。
    """
    annotations, sha = load_human_output_quality_annotations(ANNOTATIONS)
    human_scores = {
        record["input"]["run_id"]: record["annotation"]["ratings"]["completeness"]
        for record in annotations
    }
    verdicts, _ = score_judge_inputs(
        inputs, judge=ScriptedJudge(per_run=human_scores), rubric=rubric
    )
    common = {
        "rubric": rubric,
        "annotations": annotations,
        "annotation_file": "03-output-quality.jsonl",
        "annotation_sha256": sha,
        "judge_id": "scripted-judge-v1",
    }

    strict = calibrate_against_human(verdicts, **common)
    relaxed = calibrate_against_human(verdicts, allow_single_annotator=True, **common)

    assert strict.calibration_status == "insufficient_samples"
    assert strict.single_annotator_accepted is False
    # 放行之后必须给出结论，而且状态名带着"单标注者"，绝不是普通的 passed。
    assert relaxed.calibration_status == "passed_single_annotator"
    assert relaxed.calibration_status != "passed"
    assert relaxed.single_annotator_accepted is True
    assert relaxed.target_met is True
    # 放行只改结论，不改任何事实记录。
    assert relaxed.human_double_rated is False
    assert relaxed.annotator_ids == strict.annotator_ids
    assert relaxed.compared_cases == strict.compared_cases
    assert relaxed.adjacent_agreement_rate == strict.adjacent_agreement_rate


def test_single_annotator_mode_still_fails_when_agreement_is_bad(rubric, inputs) -> None:
    """放行不等于放水：一致率不达标照样是 failed。"""
    annotations, sha = load_human_output_quality_annotations(ANNOTATIONS)
    verdicts, _ = score_judge_inputs(
        inputs, judge=ScriptedJudge(score=1), rubric=rubric
    )

    report = calibrate_against_human(
        verdicts,
        rubric=rubric,
        annotations=annotations,
        annotation_file="03-output-quality.jsonl",
        annotation_sha256=sha,
        judge_id="scripted-judge-v1",
        allow_single_annotator=True,
    )

    assert report.calibration_status == "failed_single_annotator"
    assert report.target_met is False
