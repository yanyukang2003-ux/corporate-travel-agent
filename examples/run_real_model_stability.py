from __future__ import annotations

import argparse
import os

from corporate_travel_agent.agent.openai_adapter import OpenAIResponsesLanguageModel
from corporate_travel_agent.services.evaluation_model_runner import (
    run_model_intent_stability_evaluation,
)
from corporate_travel_agent.services.evaluation_performance import (
    load_model_price_table,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run the frozen D2 24-case intent subset three times with a real model. "
            "This command can create 72 billable model calls."
        )
    )
    parser.add_argument(
        "--dataset", default="data/evaluation/derived-v2"
    )
    parser.add_argument(
        "--subset", default="evals/subsets/intent-model-smoke-v1.json"
    )
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--price-table",
        default="evals/pricing/model-prices-unconfigured-v1.json",
    )
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-5.6"))
    parser.add_argument("--confirm-billable", action="store_true")
    parser.add_argument("--code-revision", default=None)
    args = parser.parse_args()

    if not args.confirm_billable:
        parser.error("--confirm-billable is required before 72 model calls")
    if not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY is not configured")
    price_table, _ = load_model_price_table(args.price_table)
    if price_table.status != "configured" or args.model not in price_table.models:
        parser.error(
            "the price table must be configured and contain the requested model"
        )
    model = OpenAIResponsesLanguageModel(model=args.model)
    summary = run_model_intent_stability_evaluation(
        dataset_directory=args.dataset,
        subset_path=args.subset,
        output_directory=args.output,
        language_model=model,
        attempts_per_case=3,
        code_revision=args.code_revision,
        price_table_version=price_table.price_table_version,
    )
    print(summary.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
