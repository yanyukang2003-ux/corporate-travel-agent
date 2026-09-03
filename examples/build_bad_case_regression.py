from __future__ import annotations

import argparse

from corporate_travel_agent.evaluation.regression import build_bad_case_dataset


def main() -> None:
    parser = argparse.ArgumentParser(description="Build versioned D6 bad-case regression data")
    parser.add_argument("--phase5-result", required=True)
    parser.add_argument("--fault-cases", required=True)
    parser.add_argument("--manual-intake", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    manifest = build_bad_case_dataset(
        phase5_result_path=args.phase5_result,
        fault_cases_path=args.fault_cases,
        manual_intake_path=args.manual_intake,
        output_directory=args.output,
    )
    print(manifest.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
