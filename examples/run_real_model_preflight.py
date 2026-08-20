from __future__ import annotations

import argparse
import os

from corporate_travel_agent.agent.openai_adapter import OpenAIResponsesLanguageModel
from corporate_travel_agent.services.evaluation_model_preflight import (
    run_real_model_preflight,
)
from corporate_travel_agent.services.evaluation_performance import (
    load_model_price_table,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run the frozen two-case real-model qualification preflight. "
            "This command makes at most two billable model calls and uses Mock providers."
        )
    )
    parser.add_argument("--dataset", default="data/evaluation/derived-v2")
    parser.add_argument(
        "--subset", default="evals/subsets/intent-model-preflight-v1.json"
    )
    parser.add_argument(
        "--parent-subset", default="evals/subsets/intent-model-smoke-v1.json"
    )
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--price-table",
        default="evals/pricing/model-prices-unconfigured-v1.json",
    )
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL"))
    parser.add_argument("--confirm-billable", action="store_true")
    parser.add_argument("--code-revision", default=None)
    args = parser.parse_args()

    if not args.confirm_billable:
        parser.error("--confirm-billable is required before model calls")
    if not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY is not configured in this process")
    model = (args.model or "").strip()
    if not model or model.casefold() in {
        "具体模型名",
        "model-name",
        "replace-with-model",
    }:
        parser.error("--model or OPENAI_MODEL must contain an actual model ID")
    price_table, _ = load_model_price_table(args.price_table)
    if price_table.status != "configured" or model not in price_table.models:
        parser.error(
            "the price table must be configured and contain the requested model"
        )

    try:
        from openai import OpenAI
    except ImportError as exc:
        parser.error(f"install the optional llm dependency: {exc}")
    client = OpenAI(max_retries=0, timeout=60.0)
    language_model = OpenAIResponsesLanguageModel(model=model, client=client)
    result = run_real_model_preflight(
        dataset_directory=args.dataset,
        subset_path=args.subset,
        parent_subset_path=args.parent_subset,
        output_directory=args.output,
        price_table_path=args.price_table,
        language_model=language_model,
        code_revision=args.code_revision,
    )
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
