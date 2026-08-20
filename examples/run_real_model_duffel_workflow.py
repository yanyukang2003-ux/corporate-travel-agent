from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from corporate_travel_agent.agent.openai_adapter import OpenAIResponsesLanguageModel
from corporate_travel_agent.providers.duffel import DuffelProvider
from corporate_travel_agent.services.evaluation_model_live_provider import (
    load_model_live_provider_dataset,
    run_model_live_provider_evaluation,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run a frozen three-attempt real OpenAI-compatible model + Duffel "
            "Test Mode workflow evaluation. It never creates an order or payment."
        )
    )
    parser.add_argument(
        "--dataset",
        default="evals/subsets/model-duffel-workflow-smoke-v1.json",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL"))
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--confirm-billable-model-calls", action="store_true")
    parser.add_argument("--confirm-external-test-calls", action="store_true")
    parser.add_argument("--code-revision", default=None)
    args = parser.parse_args()

    project_root = Path(__file__).parents[1].resolve()
    dataset, _, _, _, _ = load_model_live_provider_dataset(
        args.dataset,
        project_root=project_root,
    )
    if not args.confirm_billable_model_calls:
        parser.error("--confirm-billable-model-calls is required")
    if not args.confirm_external_test_calls:
        parser.error("--confirm-external-test-calls is required")
    if not os.getenv("OPENAI_API_KEY", "").strip():
        parser.error("OPENAI_API_KEY is not configured in this process")
    token = os.getenv("DUFFEL_ACCESS_TOKEN", "").strip()
    if not token.startswith("duffel_test_"):
        parser.error("DUFFEL_ACCESS_TOKEN must contain a Duffel Test Mode token")
    if os.getenv("DUFFEL_LIVE_MODE", "false").casefold() == "true":
        parser.error("DUFFEL_LIVE_MODE must remain false")
    if os.getenv("LIVE_BOOKING_ENABLED", "false").casefold() == "true":
        parser.error("LIVE_BOOKING_ENABLED must remain false")
    model = (args.model or "").strip()
    if model != dataset.requested_model:
        parser.error(f"model must be the frozen value {dataset.requested_model!r}, got {model!r}")
    if args.reasoning_effort != dataset.reasoning_effort:
        parser.error(f"reasoning effort must be the frozen value {dataset.reasoning_effort!r}")

    from openai import OpenAI

    client = OpenAI(max_retries=0, timeout=60.0)
    language_model = OpenAIResponsesLanguageModel(
        model=model,
        reasoning_effort=args.reasoning_effort,
        max_output_tokens=dataset.max_output_tokens_per_call,
        client=client,
    )

    def provider_factory(raw_store, currency):
        return DuffelProvider(
            token,
            api_version=os.getenv("DUFFEL_API_VERSION", "v2"),
            api_base_url=os.getenv("DUFFEL_API_BASE_URL", "https://api.duffel.com"),
            expected_currency=currency,
            raw_response_store=raw_store,
        )

    summary = run_model_live_provider_evaluation(
        dataset_path=args.dataset,
        output_directory=args.output,
        project_root=project_root,
        language_model=language_model,
        provider_factory=provider_factory,
        code_revision=args.code_revision,
    )
    print(
        json.dumps(
            {
                "evaluation_mode": summary["evaluation_mode"],
                "dataset": summary["dataset"],
                "model": summary["model"],
                "runs": summary["runs"],
                "calls": summary["calls"],
                "tokens": summary["tokens"],
                "retries": summary["retries"],
                "estimated_cost_usd": summary["estimated_cost_usd"],
                "gates": summary["gates"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not all(summary["gates"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
