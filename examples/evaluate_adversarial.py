from __future__ import annotations

import argparse
import json

from corporate_travel_agent.services.evaluation_adversarial import (
    evaluate_adversarial_run,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run the frozen D4 adversarial slice against a scripted model that obeys "
            "the attack. Measures architectural containment; makes no model calls."
        )
    )
    parser.add_argument(
        "adversarial_dataset_directory",
        nargs="?",
        default="data/evaluation/adversarial-v1",
    )
    parser.add_argument("--base-dataset", default="data/evaluation/derived-v2")
    parser.add_argument("--output", required=True)
    parser.add_argument("--attempts", type=int, default=1)
    parser.add_argument("--code-revision", default=None)
    args = parser.parse_args()

    summary = evaluate_adversarial_run(
        adversarial_dataset_directory=args.adversarial_dataset_directory,
        base_dataset_directory=args.base_dataset,
        output_directory=args.output,
        attempts_per_case=args.attempts,
        code_revision=args.code_revision,
    )
    print(
        json.dumps(
            {
                "evaluation_mode": summary.evaluation_mode,
                "posture": summary.posture,
                "selected_cases": summary.selected_cases,
                "completed_runs": summary.completed_runs,
                "delivered_attacks": summary.delivered_attacks,
                "contained_attacks": summary.contained_attacks,
                "stage_6_gate_passed": summary.stage_6_gate_passed,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
