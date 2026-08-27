"""LLM Judge：按冻结 rubric 给用户可见输出打分，并与人工标注做校准。

边界（与评测协议 §1.3、§6.1 一致）：

1. **硬规则优先。** Judge 只评价表达完整度、可操作性、政策透明度、证据落地与诚实度。
   它不能把硬断言失败改成通过；硬失败用例的分数照样记录，但一定带
   ``hard_rule_failed`` 标记，并在汇总里单独统计。
2. **可弃权。** 证据不足、用例期望自相矛盾、输出含无法核验的领域声明时，Judge 必须
   弃权而不是硬猜。弃权数进入 ``JudgeSummary.abstentions``，不按 0 分计入均值。
3. **盲评。** 候选模型名、实验分组、基线/候选标签不进入 Judge 输入。
4. **未校准不出分。** rubric 的校准状态与实际一致率一起写进汇总；没有跑过人工校准时
   ``agreement_rate`` 为 ``None``，不能用满分或 0 顶替。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from corporate_travel_agent.agent.ports import LLMCallMetadata
from corporate_travel_agent.services.evaluation_quality import (
    OUTPUT_RUBRIC_VERSION,
    JudgeInput,
    JudgeSummary,
)

MAX_RUBRIC_BYTES = 256 * 1024
MAX_JUDGE_FILE_BYTES = 32 * 1024 * 1024
ADJACENT_TOLERANCE = 1


class JudgeError(RuntimeError):
    """Judge 配置、输入或结果不合法。"""


class JudgeModel(BaseModel):
    """Judge 相关模型基类。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class RubricDimension(JudgeModel):
    """一个评分维度及其权重。"""

    id: str = Field(min_length=1, max_length=80)
    weight: float = Field(gt=0, le=1)
    question: str = Field(min_length=1, max_length=500)


class OutputQualityRubric(JudgeModel):
    """冻结的输出质量 rubric。"""

    schema_version: Literal[1] = 1
    rubric_id: str
    status: str
    purpose: str
    input_fields: tuple[str, ...]
    blind_fields: tuple[str, ...]
    dimensions: tuple[RubricDimension, ...]
    score_anchors: dict[str, str]
    hard_rule_precedence: bool
    abstain_when: tuple[str, ...]
    calibration: dict[str, Any]
    content_sha256: str

    @property
    def dimension_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.dimensions)


class JudgeDimensionScore(JudgeModel):
    """单维度评分。"""

    dimension_id: str = Field(min_length=1, max_length=80)
    score: int = Field(ge=1, le=5)
    rationale: str = Field(min_length=1, max_length=600)


class JudgeScore(JudgeModel):
    """Judge 模型的结构化输出契约。"""

    abstained: bool
    abstain_reason: str | None = Field(default=None, max_length=600)
    dimensions: list[JudgeDimensionScore]


class JudgeVerdict(JudgeModel):
    """一条可落盘、可复核的 Judge 判定。"""

    schema_version: Literal[1] = 1
    judge_case_id: str
    run_id: str
    case_id: str
    rubric_id: str
    rubric_sha256: str
    judge_id: str
    judge_model: str
    judge_prompt_version: str
    abstained: bool
    abstain_reason: str | None
    dimensions: tuple[JudgeDimensionScore, ...]
    weighted_score: float | None = Field(default=None, ge=1, le=5)
    hard_rule_failed: bool
    scored_at: datetime


class DimensionAgreement(JudgeModel):
    """单维度的人机一致率。"""

    dimension_id: str
    compared: int = Field(ge=0)
    exact: int = Field(ge=0)
    adjacent: int = Field(ge=0)
    exact_rate: float | None = Field(default=None, ge=0, le=1)
    adjacent_rate: float | None = Field(default=None, ge=0, le=1)


class CalibrationDisagreement(JudgeModel):
    """一条需要人工复核的显著分歧。"""

    run_id: str
    dimension_id: str
    judge_score: int
    human_score: int
    delta: int


class CalibrationReport(JudgeModel):
    """Judge 与人工标注的校准结果。"""

    schema_version: Literal[1] = 1
    rubric_id: str
    rubric_sha256: str
    judge_id: str
    annotation_file: str
    annotation_sha256: str
    annotator_ids: tuple[str, ...]
    human_double_rated: bool
    required_double_rated_cases: int
    compared_cases: int = Field(ge=0)
    judge_abstentions: int = Field(ge=0)
    human_abstentions: int = Field(ge=0)
    exact_agreement_rate: float | None = Field(default=None, ge=0, le=1)
    adjacent_agreement_rate: float | None = Field(default=None, ge=0, le=1)
    target_agreement: float
    target_met: bool
    # 单标注者模式的状态名单独取，绝不复用 "passed"：一致率再高，单人打的分也只能
    # 说明"评委和这个人想的差不多"，不能说明这个分数客观。名字里必须带着依据。
    single_annotator_accepted: bool = False
    calibration_status: Literal[
        "passed",
        "failed",
        "insufficient_samples",
        "passed_single_annotator",
        "failed_single_annotator",
    ]
    per_dimension: tuple[DimensionAgreement, ...]
    major_disagreements: tuple[CalibrationDisagreement, ...]


class OutputQualityJudgePort(Protocol):
    """Judge 端口：一次输入一条判定，不接触候选身份。"""

    judge_id: str
    judge_prompt_version: str

    def score(
        self,
        judge_input: JudgeInput,
        rubric: OutputQualityRubric,
    ) -> tuple[JudgeScore, LLMCallMetadata]: ...


def load_output_quality_rubric(path: str | Path) -> OutputQualityRubric:
    """加载并校验冻结 rubric；权重必须归一。"""
    rubric_path = Path(path).expanduser().resolve()
    if not rubric_path.is_file() or rubric_path.is_symlink():
        raise JudgeError(f"Rubric file is unavailable: {rubric_path}")
    content = rubric_path.read_bytes()
    if len(content) > MAX_RUBRIC_BYTES:
        raise JudgeError("Rubric file is too large")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise JudgeError(f"Rubric file is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise JudgeError("Rubric file must contain a JSON object")
    payload = dict(payload)
    payload["content_sha256"] = hashlib.sha256(content).hexdigest()
    # 严格模式不做 list→tuple 隐式转换；JSON 只能表达 list，因此在这里显式转换。
    for field in ("input_fields", "blind_fields", "dimensions", "abstain_when"):
        if isinstance(payload.get(field), list):
            payload[field] = tuple(payload[field])
    try:
        rubric = OutputQualityRubric.model_validate(payload)
    except Exception as exc:
        raise JudgeError(f"Rubric failed validation: {exc}") from exc
    if rubric.rubric_id != OUTPUT_RUBRIC_VERSION:
        raise JudgeError(
            f"Rubric id must be {OUTPUT_RUBRIC_VERSION}, got {rubric.rubric_id}"
        )
    if not rubric.dimensions:
        raise JudgeError("Rubric must define at least one dimension")
    if len({item.id for item in rubric.dimensions}) != len(rubric.dimensions):
        raise JudgeError("Rubric dimension ids must be unique")
    total_weight = sum(item.weight for item in rubric.dimensions)
    if abs(total_weight - 1.0) > 1e-6:
        raise JudgeError(f"Rubric dimension weights must sum to 1.0, got {total_weight}")
    return rubric


def load_judge_inputs(path: str | Path) -> tuple[JudgeInput, ...]:
    """读取 runner 产出的 ``judge-inputs.jsonl``。"""
    inputs_path = Path(path).expanduser().resolve()
    if not inputs_path.is_file() or inputs_path.is_symlink():
        raise JudgeError(f"Judge input file is unavailable: {inputs_path}")
    content = inputs_path.read_bytes()
    if len(content) > MAX_JUDGE_FILE_BYTES:
        raise JudgeError("Judge input file is too large")
    records: list[JudgeInput] = []
    for line_number, line in enumerate(content.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            raise JudgeError(f"Blank line in judge inputs at line {line_number}")
        try:
            records.append(JudgeInput.model_validate_json(line))
        except Exception as exc:
            raise JudgeError(
                f"Invalid judge input at line {line_number}: {exc}"
            ) from exc
    if not records:
        raise JudgeError("Judge input file contains no records")
    return tuple(records)


def assert_blinded(judge_input: JudgeInput, rubric: OutputQualityRubric) -> None:
    """确认 Judge 输入里没有携带被 rubric 列为盲字段的信息。"""
    if not judge_input.candidate_identity_blinded:
        raise JudgeError(f"Judge input {judge_input.judge_case_id} is not blinded")
    payload = judge_input.model_dump(mode="json")
    for field in rubric.blind_fields:
        if field in payload:
            raise JudgeError(
                f"Judge input {judge_input.judge_case_id} leaks blind field {field}"
            )


def score_judge_inputs(
    judge_inputs: Iterable[JudgeInput],
    *,
    judge: OutputQualityJudgePort,
    rubric: OutputQualityRubric,
    hard_failed_case_ids: frozenset[str] = frozenset(),
) -> tuple[tuple[JudgeVerdict, ...], tuple[LLMCallMetadata, ...]]:
    """逐条打分；硬失败用例照打分，但结果永远带硬失败标记。"""
    verdicts: list[JudgeVerdict] = []
    call_metadata: list[LLMCallMetadata] = []
    for judge_input in judge_inputs:
        assert_blinded(judge_input, rubric)
        score, metadata = judge.score(judge_input, rubric)
        _validate_score(score, rubric, judge_input.judge_case_id)
        weighted = None if score.abstained else _weighted_score(score, rubric)
        verdicts.append(
            JudgeVerdict(
                judge_case_id=judge_input.judge_case_id,
                run_id=judge_input.run_id,
                case_id=judge_input.case_id,
                rubric_id=rubric.rubric_id,
                rubric_sha256=rubric.content_sha256,
                judge_id=judge.judge_id,
                judge_model=metadata.model,
                judge_prompt_version=judge.judge_prompt_version,
                abstained=score.abstained,
                abstain_reason=score.abstain_reason,
                dimensions=tuple(score.dimensions),
                weighted_score=weighted,
                hard_rule_failed=judge_input.case_id in hard_failed_case_ids,
                scored_at=datetime.now(UTC),
            )
        )
        call_metadata.append(metadata)
    return tuple(verdicts), tuple(call_metadata)


def mean_judge_score(verdicts: Iterable[JudgeVerdict]) -> float | None:
    """弃权不计入均值；全部弃权时返回 ``None`` 而不是 0。"""
    scored = [
        item.weighted_score
        for item in verdicts
        if not item.abstained and item.weighted_score is not None
    ]
    return sum(scored) / len(scored) if scored else None


def summarize_judge_verdicts(
    verdicts: Iterable[JudgeVerdict],
    *,
    rubric: OutputQualityRubric,
    calibration: CalibrationReport | None = None,
) -> JudgeSummary:
    """汇总为协议要求的 ``JudgeSummary``。"""
    items = tuple(verdicts)
    if not items:
        raise JudgeError("A judge summary requires at least one verdict")
    judge_ids = {item.judge_id for item in items}
    if len(judge_ids) != 1:
        raise JudgeError("Verdicts from different judges must not be merged")
    return JudgeSummary(
        judge_id=judge_ids.pop(),
        rubric_version=rubric.rubric_id,
        calibration_set=(
            calibration.annotation_file if calibration is not None else None
        ),
        agreement_rate=(
            calibration.adjacent_agreement_rate if calibration is not None else None
        ),
        abstentions=sum(item.abstained for item in items),
    )


def load_human_output_quality_annotations(
    path: str | Path,
) -> tuple[tuple[Mapping[str, Any], ...], str]:
    """读取人工输出质量标注，返回记录与文件哈希。"""
    annotation_path = Path(path).expanduser().resolve()
    if not annotation_path.is_file() or annotation_path.is_symlink():
        raise JudgeError(f"Annotation file is unavailable: {annotation_path}")
    content = annotation_path.read_bytes()
    if len(content) > MAX_JUDGE_FILE_BYTES:
        raise JudgeError("Annotation file is too large")
    records: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(content.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise JudgeError(
                f"Invalid annotation JSON at line {line_number}: {exc}"
            ) from exc
        if not isinstance(record, dict):
            raise JudgeError(f"Annotation at line {line_number} is not an object")
        if record.get("annotation_task") != "output_quality":
            raise JudgeError(
                f"Annotation at line {line_number} is not an output_quality record"
            )
        records.append(record)
    if not records:
        raise JudgeError("Annotation file contains no records")
    return tuple(records), hashlib.sha256(content).hexdigest()


def calibrate_against_human(
    verdicts: Iterable[JudgeVerdict],
    *,
    rubric: OutputQualityRubric,
    annotations: tuple[Mapping[str, Any], ...],
    annotation_file: str,
    annotation_sha256: str,
    judge_id: str,
    allow_single_annotator: bool = False,
) -> CalibrationReport:
    """按 run_id 对齐人机评分，报告严格一致与相邻一致率。

    ``allow_single_annotator`` 显式放行"只有一个标注者"这一道门，由调用方承担。
    放行后状态会用 ``*_single_annotator`` 命名，永远不会显示成普通的 ``passed``。
    """
    by_run = {item.run_id: item for item in verdicts}
    target = float(rubric.calibration.get("target_exact_or_adjacent_agreement", 0.9))
    required = int(rubric.calibration.get("required_human_double_rated_cases", 20))

    annotator_ids: set[str] = set()
    rounds: set[int] = set()
    per_dimension: dict[str, dict[str, int]] = {
        dimension.id: {"compared": 0, "exact": 0, "adjacent": 0}
        for dimension in rubric.dimensions
    }
    disagreements: list[CalibrationDisagreement] = []
    compared_cases = 0
    judge_abstentions = 0
    human_abstentions = 0
    total_compared = 0
    total_exact = 0
    total_adjacent = 0

    for record in annotations:
        meta = record.get("annotation_meta") or {}
        if isinstance(meta, dict):
            annotator = meta.get("annotator_id")
            if isinstance(annotator, str):
                annotator_ids.add(annotator)
            round_value = meta.get("round")
            if isinstance(round_value, int):
                rounds.add(round_value)
        run_id = ((record.get("input") or {}) or {}).get("run_id")
        if not isinstance(run_id, str) or run_id not in by_run:
            continue
        annotation = record.get("annotation") or {}
        if not isinstance(annotation, dict):
            continue
        if annotation.get("abstain"):
            human_abstentions += 1
            continue
        verdict = by_run[run_id]
        if verdict.abstained:
            judge_abstentions += 1
            continue
        ratings = annotation.get("ratings") or {}
        if not isinstance(ratings, dict):
            continue
        judge_scores = {item.dimension_id: item.score for item in verdict.dimensions}
        matched_any = False
        for dimension in rubric.dimensions:
            human_score = ratings.get(dimension.id)
            judge_score = judge_scores.get(dimension.id)
            if not isinstance(human_score, int) or judge_score is None:
                continue
            matched_any = True
            delta = judge_score - human_score
            bucket = per_dimension[dimension.id]
            bucket["compared"] += 1
            total_compared += 1
            if delta == 0:
                bucket["exact"] += 1
                total_exact += 1
            if abs(delta) <= ADJACENT_TOLERANCE:
                bucket["adjacent"] += 1
                total_adjacent += 1
            else:
                disagreements.append(
                    CalibrationDisagreement(
                        run_id=run_id,
                        dimension_id=dimension.id,
                        judge_score=judge_score,
                        human_score=human_score,
                        delta=delta,
                    )
                )
        if matched_any:
            compared_cases += 1

    exact_rate = total_exact / total_compared if total_compared else None
    adjacent_rate = total_adjacent / total_compared if total_compared else None
    # 人工双评的判定只看标注元数据，不靠推断：同一份用例必须有两个标注者或两轮。
    human_double_rated = len(annotator_ids) > 1 or len(rounds) > 1
    status: str
    if allow_single_annotator and not human_double_rated:
        # 样本量按**人工标注条数**算，而不是按"评委没弃权的条数"算：弃权是评分表
        # 明确允许的行为（弃权≠0分），不该反过来把数据集判成太小。
        labelled = compared_cases + judge_abstentions + human_abstentions
        if labelled < required:
            status = "insufficient_samples"
            target_met = False
        elif adjacent_rate is not None and adjacent_rate >= target:
            status = "passed_single_annotator"
            target_met = True
        else:
            status = "failed_single_annotator"
            target_met = False
    elif compared_cases < required or not human_double_rated:
        status = "insufficient_samples"
        target_met = False
    elif adjacent_rate is not None and adjacent_rate >= target:
        status = "passed"
        target_met = True
    else:
        status = "failed"
        target_met = False

    return CalibrationReport(
        rubric_id=rubric.rubric_id,
        rubric_sha256=rubric.content_sha256,
        judge_id=judge_id,
        annotation_file=annotation_file,
        annotation_sha256=annotation_sha256,
        annotator_ids=tuple(sorted(annotator_ids)),
        human_double_rated=human_double_rated,
        required_double_rated_cases=required,
        compared_cases=compared_cases,
        judge_abstentions=judge_abstentions,
        human_abstentions=human_abstentions,
        exact_agreement_rate=exact_rate,
        adjacent_agreement_rate=adjacent_rate,
        target_agreement=target,
        target_met=target_met,
        single_annotator_accepted=bool(allow_single_annotator and not human_double_rated),
        calibration_status=status,
        per_dimension=tuple(
            DimensionAgreement(
                dimension_id=dimension_id,
                compared=bucket["compared"],
                exact=bucket["exact"],
                adjacent=bucket["adjacent"],
                exact_rate=(
                    bucket["exact"] / bucket["compared"] if bucket["compared"] else None
                ),
                adjacent_rate=(
                    bucket["adjacent"] / bucket["compared"]
                    if bucket["compared"]
                    else None
                ),
            )
            for dimension_id, bucket in per_dimension.items()
        ),
        major_disagreements=tuple(disagreements),
    )


def _validate_score(
    score: JudgeScore,
    rubric: OutputQualityRubric,
    judge_case_id: str,
) -> None:
    if score.abstained:
        if not score.abstain_reason:
            raise JudgeError(f"Judge abstained on {judge_case_id} without a reason")
        return
    scored_ids = [item.dimension_id for item in score.dimensions]
    if len(set(scored_ids)) != len(scored_ids):
        raise JudgeError(f"Judge repeated a dimension on {judge_case_id}")
    missing = sorted(set(rubric.dimension_ids) - set(scored_ids))
    if missing:
        raise JudgeError(
            f"Judge omitted dimensions on {judge_case_id}: {', '.join(missing)}"
        )
    unknown = sorted(set(scored_ids) - set(rubric.dimension_ids))
    if unknown:
        raise JudgeError(
            f"Judge scored unknown dimensions on {judge_case_id}: {', '.join(unknown)}"
        )


def _weighted_score(score: JudgeScore, rubric: OutputQualityRubric) -> float:
    weights = {item.id: item.weight for item in rubric.dimensions}
    return sum(item.score * weights[item.dimension_id] for item in score.dimensions)
