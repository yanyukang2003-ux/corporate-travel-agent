from __future__ import annotations

import argparse

from corporate_travel_agent.services.evaluation_regression import (
    evaluate_regression,
    freeze_regression_baseline,
)


def _artifact_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--quality", required=True)
    parser.add_argument("--trajectory", required=True)
    parser.add_argument("--efficiency", required=True)
    parser.add_argument("--recovery", required=True)
    parser.add_argument("--performance", required=True)


def _artifacts(args: argparse.Namespace) -> dict[str, str]:
    return {
        "quality": args.quality,
        "trajectory": args.trajectory,
        "efficiency": args.efficiency,
        "recovery": args.recovery,
        "performance": args.performance,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze or compare Agent evaluation baselines")
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze = subparsers.add_parser("freeze")
    _artifact_arguments(freeze)
    freeze.add_argument("--baseline", required=True)
    compare = subparsers.add_parser("compare")
    _artifact_arguments(compare)
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--output", required=True)
    compare.add_argument("--initialization-self-check", action="store_true")
    args = parser.parse_args()
    if args.command == "freeze":
        result = freeze_regression_baseline(
            artifact_paths=_artifacts(args), output_path=args.baseline
        )
    else:
        result = evaluate_regression(
            baseline_path=args.baseline,
            current_artifact_paths=_artifacts(args),
            output_directory=args.output,
            comparison_mode=(
                "baseline_initialization_self_check"
                if args.initialization_self_check
                else "candidate_regression"
            ),
        )
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
