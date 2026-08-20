#!/usr/bin/env python3
"""Run D4 agent-eval model_mock smoke (real LLM intent + Mock provider).

Default subset is the frozen 24-case agent-eval-model-smoke-v1. Providers stay
deterministic Mock; only llm.extract_trip_intent is billable. Hard assertions
score the resulting task against the frozen D4 labels.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from corporate_travel_agent.agent.openai_adapter import OpenAIResponsesLanguageModel
from corporate_travel_agent.services.evaluation_agent_eval import (
    load_agent_eval_subset,
    run_agent_eval_dataset,
    write_run_report,
)
from corporate_travel_agent.services.evaluation_performance import load_model_price_table


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("data/evaluation/agent-eval-v1"),
        help="Path to frozen agent-eval-v1 dataset root",
    )
    parser.add_argument(
        "--subset",
        type=Path,
        default=Path("evals/subsets/agent-eval-model-smoke-v1.json"),
        help="Frozen subset JSON (smoke 24 or preflight 2)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New output directory (must not already exist)",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=1,
        help="Attempts per case (use 3 for stability; default 1 for smoke)",
    )
    parser.add_argument("--price-table", required=True)
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL"))
    parser.add_argument(
        "--confirm-billable",
        action="store_true",
        help="Required acknowledgement that this run makes billable model calls",
    )
    parser.add_argument("--code-revision", default=None)
    parser.add_argument(
        "--require-frozen-dataset",
        action="store_true",
        default=True,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    if not args.confirm_billable:
        parser.error("--confirm-billable is required before model calls")
    if not os.getenv("OPENAI_API_KEY"):
        parser.error("OPENAI_API_KEY is not configured in this process")
    model = (args.model or "").strip()
    if not model:
        parser.error("--model or OPENAI_MODEL must contain an actual model ID")
    if args.attempts < 1:
        parser.error("--attempts must be at least 1")

    price_table, _ = load_model_price_table(args.price_table)
    if price_table.status != "configured" or model not in price_table.models:
        parser.error("the price table must be configured and contain the requested model")

    subset = load_agent_eval_subset(args.subset)
    if subset.get("status") != "frozen":
        parser.error("the subset must be frozen before a billable run")
    case_ids = tuple(subset["case_ids"])

    manifest = json.loads((args.dataset / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") != "frozen":
        parser.error("agent-eval-v1 dataset must be frozen before a billable model run")
    if subset.get("source_case_file_sha256") != manifest.get("case_file_sha256"):
        parser.error(
            "subset source_case_file_sha256 does not match dataset case_file_sha256; "
            "rebuild or refresh the subset fingerprints"
        )

    output = args.output.expanduser().resolve()
    if output.exists():
        parser.error(f"output directory already exists: {output}")

    from openai import OpenAI

    client = OpenAI(max_retries=0, timeout=60.0)
    language_model = OpenAIResponsesLanguageModel(model=model, client=client)

    expected_calls = len(case_ids) * args.attempts
    print(
        json.dumps(
            {
                "status": "starting",
                "mode": "model_mock",
                "subset_id": subset.get("subset_id"),
                "selected_cases": len(case_ids),
                "attempts_per_case": args.attempts,
                "expected_model_calls_min": expected_calls,
                "model": model,
                "price_table_version": price_table.price_table_version,
                "dataset_version": manifest.get("dataset_version"),
            },
            indent=2,
        )
    )

    summary = run_agent_eval_dataset(
        args.dataset,
        mode="model_mock",
        case_ids=case_ids,
        language_model=language_model,
        attempts_per_case=args.attempts,
        subset_id=str(subset.get("subset_id") or ""),
        price_table=price_table,
    )
    # Attach price table version into report payload via notes on disk.
    report = write_run_report(summary, output)
    meta_path = output / "model-run-meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "mode": "model_mock",
                "subset_id": subset.get("subset_id"),
                "subset_sha256": subset.get("_sha256"),
                "requested_model": model,
                "price_table_version": price_table.price_table_version,
                "price_table_path": str(args.price_table),
                "currency": price_table.currency,
                "code_revision": args.code_revision,
                "confirm_billable": True,
                "provider_mode": "MOCK",
                "tool_choice_exposure": "orchestrator_controlled",
                "input_tokens_total": summary.input_tokens_total,
                "output_tokens_total": summary.output_tokens_total,
                "estimated_cost_usd_total": summary.estimated_cost_usd_total,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "mode": summary.mode,
                "subset_id": summary.subset_id,
                "selected_cases": summary.selected_cases,
                "attempts_per_case": summary.attempts_per_case,
                "completed_runs": summary.completed_runs,
                "passed_cases": summary.passed_cases,
                "failed_cases": summary.failed_cases,
                "error_cases": summary.error_cases,
                "real_model_calls": summary.real_model_calls,
                "input_tokens_total": summary.input_tokens_total,
                "output_tokens_total": summary.output_tokens_total,
                "estimated_cost_usd_total": summary.estimated_cost_usd_total,
                "currency": summary.currency or price_table.currency,
                "assertion_pass_rate": summary.assertion_pass_rate,
                "report": str(report),
                "cost_ledger": str(output / "cost-ledger.jsonl"),
                "cost_summary": str(output / "cost-summary.json"),
                "meta": str(meta_path),
            },
            indent=2,
        )
    )
    if summary.failed_cases or summary.error_cases:
        print(f"details={output / 'failed-assertions.jsonl'}")


if __name__ == "__main__":
    main()
