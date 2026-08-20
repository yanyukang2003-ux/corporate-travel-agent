from __future__ import annotations

import argparse

from corporate_travel_agent.services.evaluation_runner import (
    run_deterministic_workflow_evaluation,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a version-pinned deterministic workflow evaluation with JSONL traces."
    )
    parser.add_argument(
        "dataset_directory",
        nargs="?",
        default="data/evaluation/derived-v2",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--case-id", action="append", default=None)
    parser.add_argument("--code-revision", default=None)
    args = parser.parse_args()

    summary = run_deterministic_workflow_evaluation(
        args.dataset_directory,
        args.output,
        attempts_per_case=args.attempts,
        case_ids=tuple(args.case_id) if args.case_id else None,
        code_revision=args.code_revision,
    )
    print(summary.model_dump_json(indent=2))


if __name__ == "__main__":
    main()

