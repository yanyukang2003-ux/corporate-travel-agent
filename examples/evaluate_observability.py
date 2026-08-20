from __future__ import annotations

import argparse

from corporate_travel_agent.services.evaluation_observability import (
    build_observability_evaluation,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build local observation, alert, and final evaluation reports"
    )
    parser.add_argument("--quality", required=True)
    parser.add_argument("--trajectory", required=True)
    parser.add_argument("--efficiency", required=True)
    parser.add_argument("--recovery", required=True)
    parser.add_argument("--performance", required=True)
    parser.add_argument("--regression", required=True)
    parser.add_argument("--d6-manifest", required=True)
    parser.add_argument("--d6-candidates", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--price-table", required=True)
    parser.add_argument("--observation-data", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = build_observability_evaluation(
        stage_result_paths={
            "quality": args.quality,
            "trajectory": args.trajectory,
            "efficiency": args.efficiency,
            "recovery": args.recovery,
            "performance": args.performance,
        },
        regression_result_path=args.regression,
        d6_manifest_path=args.d6_manifest,
        d6_candidates_path=args.d6_candidates,
        baseline_path=args.baseline,
        price_table_path=args.price_table,
        observation_data_directory=args.observation_data,
        output_directory=args.output,
    )
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
