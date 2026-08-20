from __future__ import annotations

import argparse

from corporate_travel_agent.services.evaluation_recovery import (
    evaluate_fault_recovery_run,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the frozen D5 fault recovery and safe degradation evaluation."
    )
    parser.add_argument(
        "fault_dataset_directory",
        nargs="?",
        default="data/evaluation/fault-eval-v1",
    )
    parser.add_argument(
        "--base-dataset",
        default="data/evaluation/derived-v2",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--code-revision")
    args = parser.parse_args()
    summary = evaluate_fault_recovery_run(
        fault_dataset_directory=args.fault_dataset_directory,
        base_dataset_directory=args.base_dataset,
        output_directory=args.output,
        code_revision=args.code_revision,
    )
    print(summary.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
