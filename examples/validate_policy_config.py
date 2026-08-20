from __future__ import annotations

import argparse
import json
import os

from corporate_travel_agent.services.policy_config import load_policy_configuration


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate an enterprise travel policy without starting the API."
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=os.getenv("POLICY_CONFIG_FILE", "config/travel-policy.json"),
    )
    args = parser.parse_args()
    loaded = load_policy_configuration(args.path)
    print(
        json.dumps(
            {
                "valid": True,
                "config_version": loaded.config.config_version,
                "active_policy_snapshot": loaded.active_policy.snapshot_id,
                "employees": len(loaded.employee_snapshots),
                "approvers": len(loaded.config.approvers),
                "cities": len(loaded.config.cities),
                "policy_snapshots": len(loaded.policy_snapshots),
                "sha256": loaded.sha256,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
