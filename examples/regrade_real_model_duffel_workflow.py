from __future__ import annotations

import argparse
import json
from pathlib import Path

from corporate_travel_agent.services.evaluation_model_live_provider import (
    regrade_model_live_provider_evaluation,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Regrade an existing real-model + Duffel evaluation from its local "
            "traces. This command makes no external API calls."
        )
    )
    parser.add_argument(
        "--dataset",
        default="evals/subsets/model-duffel-workflow-smoke-v1.json",
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    project_root = Path(__file__).parents[1].resolve()
    summary = regrade_model_live_provider_evaluation(
        dataset_path=args.dataset,
        output_directory=args.output,
        project_root=project_root,
    )
    print(
        json.dumps(
            {
                "grader_version": summary["grader_version"],
                "runs": summary["runs"],
                "hallucination": summary["hallucination"],
                "operational_reliability": summary["operational_reliability"],
                "resource_accounting": summary["resource_accounting"],
                "gates": summary["gates"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
