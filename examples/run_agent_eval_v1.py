#!/usr/bin/env python3
"""Run D4 agent-eval-v1 hard-assertion baseline."""

from __future__ import annotations

import argparse
from pathlib import Path

from corporate_travel_agent.services.evaluation_agent_eval import (
    load_agent_eval_subset,
    run_agent_eval_dataset,
    write_run_report,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("data/evaluation/agent-eval-v1"),
        help="Path to agent-eval-v1 dataset root",
    )
    parser.add_argument(
        "--mode",
        choices=["deterministic_live", "oracle_label"],
        default="deterministic_live",
        help=(
            "deterministic_live: drive orchestrator with world fixtures; "
            "oracle_label: score label-faithful observations (executor smoke)"
        ),
    )
    parser.add_argument(
        "--subset",
        type=Path,
        default=None,
        help=(
            "Optional fixed subset JSON (e.g. evals/subsets/agent-eval-model-smoke-v1.json). "
            "Runs only that case_id list in listed order."
        ),
    )
    parser.add_argument("--case-id", action="append", default=None)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/evaluation-runs/d4-agent-eval-latest"),
    )
    args = parser.parse_args()

    case_ids: tuple[str, ...] | None = None
    subset_id = None
    if args.subset is not None:
        if args.case_id:
            parser.error("--subset and --case-id are mutually exclusive")
        subset = load_agent_eval_subset(args.subset)
        case_ids = tuple(subset["case_ids"])
        subset_id = subset.get("subset_id")
    elif args.case_id:
        case_ids = tuple(args.case_id)

    summary = run_agent_eval_dataset(
        args.dataset,
        mode=args.mode,
        case_ids=case_ids,
    )
    report = write_run_report(summary, args.output)
    subset_note = f" subset={subset_id}" if subset_id else ""
    print(
        f"mode={summary.mode}{subset_note} "
        f"passed={summary.passed_cases}/{summary.selected_cases} "
        f"errors={summary.error_cases} assertion_pass_rate={summary.assertion_pass_rate}"
    )
    print(f"report={report}")
    if summary.failed_cases or summary.error_cases:
        print(f"details={args.output / 'failed-assertions.jsonl'}")


if __name__ == "__main__":
    main()
