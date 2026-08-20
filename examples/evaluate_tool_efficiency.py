from __future__ import annotations

import argparse

from corporate_travel_agent.services.evaluation_efficiency import (
    evaluate_tool_efficiency_run,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate invalid, duplicate, redundant, and no-gain tool calls."
    )
    parser.add_argument(
        "dataset_directory",
        nargs="?",
        default="data/evaluation/derived-v2",
    )
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--trajectory-run", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--tool-registry",
        default="evals/contracts/tool-registry-v1.json",
    )
    args = parser.parse_args()

    summary = evaluate_tool_efficiency_run(
        dataset_directory=args.dataset_directory,
        source_run_directory=args.source_run,
        trajectory_run_directory=args.trajectory_run,
        output_directory=args.output,
        tool_registry_path=args.tool_registry,
    )
    print(summary.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
