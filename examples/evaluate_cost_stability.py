from __future__ import annotations

import argparse

from corporate_travel_agent.services.evaluation_performance import (
    evaluate_performance_and_stability,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate token, latency, cost, and repeated-run stability from a "
            "version-pinned evaluation run."
        )
    )
    parser.add_argument("source_run_directory")
    parser.add_argument("--output", required=True)
    parser.add_argument("--price-table", default=None)
    parser.add_argument("--expected-attempts", type=int, default=3)
    args = parser.parse_args()

    summary = evaluate_performance_and_stability(
        source_run_directory=args.source_run_directory,
        output_directory=args.output,
        price_table_path=args.price_table,
        expected_attempts=args.expected_attempts,
    )
    print(summary.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
