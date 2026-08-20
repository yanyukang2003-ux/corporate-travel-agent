"""Import a policy configuration JSON document into PostgreSQL."""

from __future__ import annotations

import argparse
import json
import os
import sys

from corporate_travel_agent.services.config_repository import (
    import_policy_configuration_to_database,
)
from corporate_travel_agent.services.db_engine import create_database_engine
from corporate_travel_agent.services.policy_config import load_policy_configuration
from corporate_travel_agent.services.sqlalchemy_repository import SQLAlchemyTaskRepository


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file",
        default=os.getenv("POLICY_CONFIG_FILE") or os.getenv("POLICY_CONFIG_BOOTSTRAP_FILE"),
        help="Policy config JSON path (default: POLICY_CONFIG_FILE / BOOTSTRAP)",
    )
    parser.add_argument(
        "--activate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Mark this configuration as the active one (default: true)",
    )
    args = parser.parse_args(argv)
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    if not args.file:
        print("--file or POLICY_CONFIG_FILE is required", file=sys.stderr)
        return 2

    loaded = load_policy_configuration(args.file)
    engine = create_database_engine(database_url)
    try:
        repository = SQLAlchemyTaskRepository(database_url, engine=engine)
        repository.check_connection()
        repository.check_schema()
        config_id = import_policy_configuration_to_database(
            engine,
            loaded,
            activate=args.activate,
        )
    finally:
        engine.dispose()

    print(
        json.dumps(
            {
                "config_id": config_id,
                "config_version": loaded.config.config_version,
                "sha256": loaded.sha256,
                "activated": args.activate,
                "source": loaded.source_identifier,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
