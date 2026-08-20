from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from corporate_travel_agent.agent.openai_adapter import OpenAIResponsesLanguageModel
from corporate_travel_agent.services.evaluation_performance import (
    load_model_price_table,
)
from corporate_travel_agent.services.evaluation_runner import (
    run_model_workflow_evaluation,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run the frozen D1 workflow subset with a real model doing intent "
            "extraction. Providers stay deterministic Mock; this command makes "
            "records x attempts billable model calls."
        )
    )
    parser.add_argument("--dataset", default="data/evaluation/derived-v2")
    parser.add_argument("--subset", default="evals/subsets/workflow-model-smoke-v1.json")
    parser.add_argument("--output", required=True)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--price-table", required=True)
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL"))
    parser.add_argument("--confirm-billable", action="store_true")
    parser.add_argument("--code-revision", default=None)
    args = parser.parse_args()

    if not args.confirm_billable:
        parser.error("--confirm-billable is required before model calls")
    if not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY is not configured in this process")
    model = (args.model or "").strip()
    if not model:
        parser.error("--model or OPENAI_MODEL must contain an actual model ID")
    price_table, _ = load_model_price_table(args.price_table)
    if price_table.status != "configured" or model not in price_table.models:
        parser.error("the price table must be configured and contain the requested model")

    subset = json.loads(Path(args.subset).read_text(encoding="utf-8"))
    if subset.get("status") != "frozen":
        parser.error("the workflow subset must be frozen before a billable run")
    case_ids = tuple(subset["case_ids"])
    if len(case_ids) != subset["records"]:
        parser.error("subset records do not match the case ID count")

    from openai import OpenAI

    client = OpenAI(max_retries=0, timeout=60.0)
    language_model = OpenAIResponsesLanguageModel(model=model, client=client)
    summary = run_model_workflow_evaluation(
        args.dataset,
        args.output,
        language_model=language_model,
        attempts_per_case=args.attempts,
        case_ids=case_ids,
        code_revision=args.code_revision,
        price_table_version=price_table.price_table_version,
    )
    print(
        json.dumps(
            {
                "evaluation_mode": summary.evaluation_mode,
                "selected_cases": summary.selected_cases,
                "expected_runs": summary.expected_runs,
                "completed_runs": summary.completed_runs,
                "passed_runs": summary.passed_runs,
                "real_model_calls": summary.real_model_calls,
                "stage_2_rule_gate_passed": summary.stage_2_rule_gate_passed,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
