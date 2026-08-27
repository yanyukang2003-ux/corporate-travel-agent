#!/usr/bin/env python3
"""对已有评测运行的 ``judge-inputs.jsonl`` 执行 LLM Judge，并与人工标注校准。

它不重跑工作流，只读取既有运行目录的 Judge 输入，因此不会重复消耗 Provider 或改写
原始报告。Judge 调用是**计费**的，必须显式确认。

```bash
.venv/bin/python examples/run_output_quality_judge.py \
  --run-directory reports/evaluation-runs/phase2-quality-20260802 \
  --model "$OPENAI_MODEL" \
  --confirm-billable-judge-calls \
  --output reports/evaluation-runs/<judge-run-id>
```

不带 ``--model`` 时可用 ``--dry-run`` 只做输入体检（盲字段、维度契约、可对齐的人工
标注条数），不发起任何模型调用。
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from corporate_travel_agent.services.evaluation_judge import (
    JudgeError,
    assert_blinded,
    calibrate_against_human,
    load_human_output_quality_annotations,
    load_judge_inputs,
    load_output_quality_rubric,
    mean_judge_score,
    score_judge_inputs,
    summarize_judge_verdicts,
)

REPO = Path(__file__).resolve().parents[1]
DEFAULT_RUBRIC = REPO / "evals/rubrics/output-quality-v1.json"
DEFAULT_ANNOTATIONS = (
    REPO / "data/evaluation/human-annotations/v1/to-label/03-output-quality.jsonl"
)


def _hard_failed_case_ids(run_directory: Path) -> frozenset[str]:
    """从既有评测结果里读出硬失败用例，供 Judge 结果标注硬规则优先。"""
    result_path = run_directory / "evaluation-result.json"
    if not result_path.is_file():
        return frozenset()
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    return frozenset(
        str(item.get("case_id"))
        for item in payload.get("hard_failures") or ()
        if item.get("case_id")
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score an existing evaluation run's user-visible outputs with an LLM judge."
    )
    parser.add_argument("--run-directory", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--rubric", default=str(DEFAULT_RUBRIC))
    parser.add_argument("--annotations", default=str(DEFAULT_ANNOTATIONS))
    parser.add_argument("--model", default=None)
    parser.add_argument(
        "--reasoning-effort",
        default="medium",
        choices=("none", "low", "medium", "high", "xhigh", "max"),
    )
    parser.add_argument("--judge-id", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--confirm-billable-judge-calls",
        action="store_true",
        help="Required before any billed judge call is made.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate judge inputs and calibration alignment without calling a model.",
    )
    args = parser.parse_args()

    run_directory = Path(args.run_directory)
    output_root = Path(args.output)
    if output_root.exists():
        raise SystemExit(f"Output directory must not exist yet: {output_root}")

    rubric = load_output_quality_rubric(args.rubric)
    judge_inputs = load_judge_inputs(run_directory / "judge-inputs.jsonl")
    if args.limit is not None:
        judge_inputs = judge_inputs[: args.limit]
    for judge_input in judge_inputs:
        assert_blinded(judge_input, rubric)

    annotations, annotation_sha256 = load_human_output_quality_annotations(
        args.annotations
    )
    annotated_run_ids = {
        ((record.get("input") or {}) or {}).get("run_id") for record in annotations
    }
    alignable = sum(1 for item in judge_inputs if item.run_id in annotated_run_ids)

    if args.dry_run:
        summary: dict[str, Any] = {
            "schema_version": 1,
            "mode": "dry_run",
            "generated_at": datetime.now(UTC).isoformat(),
            "run_directory": str(run_directory),
            "rubric_id": rubric.rubric_id,
            "rubric_sha256": rubric.content_sha256,
            "rubric_status": rubric.status,
            "judge_inputs": len(judge_inputs),
            "all_inputs_blinded": True,
            "dimensions": list(rubric.dimension_ids),
            "human_annotations": len(annotations),
            "annotation_sha256": annotation_sha256,
            "alignable_calibration_cases": alignable,
            "required_calibration_cases": int(
                rubric.calibration.get("required_human_double_rated_cases", 20)
            ),
            "model_calls": 0,
        }
        output_root.mkdir(parents=True)
        (output_root / "judge-dry-run.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return

    if not args.model:
        raise SystemExit("--model is required unless --dry-run is used")
    if not args.confirm_billable_judge_calls:
        raise SystemExit(
            "Judge calls are billable. Re-run with --confirm-billable-judge-calls."
        )

    from corporate_travel_agent.services.evaluation_judge_openai import (
        OpenAIOutputQualityJudge,
    )

    judge = OpenAIOutputQualityJudge(
        model=args.model,
        judge_id=args.judge_id,
        reasoning_effort=args.reasoning_effort,
    )
    verdicts, call_metadata = score_judge_inputs(
        judge_inputs,
        judge=judge,
        rubric=rubric,
        hard_failed_case_ids=_hard_failed_case_ids(run_directory),
    )
    calibration = calibrate_against_human(
        verdicts,
        rubric=rubric,
        annotations=annotations,
        annotation_file=str(Path(args.annotations).name),
        annotation_sha256=annotation_sha256,
        judge_id=judge.judge_id,
    )
    judge_summary = summarize_judge_verdicts(
        verdicts, rubric=rubric, calibration=calibration
    )

    output_root.mkdir(parents=True)
    (output_root / "judge-verdicts.jsonl").write_text(
        "".join(f"{item.model_dump_json()}\n" for item in verdicts), encoding="utf-8"
    )
    (output_root / "judge-calibration.json").write_text(
        calibration.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    summary = {
        "schema_version": 1,
        "mode": "llm_judge",
        "generated_at": datetime.now(UTC).isoformat(),
        "run_directory": str(run_directory),
        "rubric_id": rubric.rubric_id,
        "rubric_sha256": rubric.content_sha256,
        "judge_id": judge.judge_id,
        "judge_prompt_version": judge.judge_prompt_version,
        "judged_runs": len(verdicts),
        "abstentions": judge_summary.abstentions,
        "hard_rule_failed_runs": sum(item.hard_rule_failed for item in verdicts),
        "mean_judge_quality_score": mean_judge_score(verdicts),
        "calibration_status": calibration.calibration_status,
        "exact_agreement_rate": calibration.exact_agreement_rate,
        "adjacent_agreement_rate": calibration.adjacent_agreement_rate,
        "target_agreement": calibration.target_agreement,
        "human_double_rated": calibration.human_double_rated,
        "model_calls": len(call_metadata),
        "total_tokens": sum(item.total_tokens or 0 for item in call_metadata) or None,
        "limitations": [
            "The judge never overrides deterministic hard assertions.",
            "Abstentions are excluded from the mean, never scored as 0.",
            "A judge score is uncalibrated until calibration_status is 'passed'.",
        ],
    }
    (output_root / "run-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except JudgeError as exc:
        raise SystemExit(f"Judge error: {exc}") from exc
