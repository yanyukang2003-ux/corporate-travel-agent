from __future__ import annotations

import argparse
import json
import os
from collections import Counter

from corporate_travel_agent.services.object_storage import LocalRawResponseObjectStore
from corporate_travel_agent.services.replay_dataset import (
    build_task_replay_cases,
    export_replay_dataset,
)
from corporate_travel_agent.services.sqlalchemy_repository import SQLAlchemyTaskRepository


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export one persisted task as a write-once, redacted Replay dataset. "
            "The command never calls a provider or language model."
        )
    )
    parser.add_argument("task_id")
    parser.add_argument("output_directory")
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--dataset-version", default="1")
    args = parser.parse_args()

    database_url = os.getenv("DATABASE_URL")
    raw_store_directory = os.getenv("RAW_RESPONSE_STORE_DIR")
    if not database_url:
        raise SystemExit("Set DATABASE_URL before exporting a Replay dataset")
    if not raw_store_directory:
        raise SystemExit("Set RAW_RESPONSE_STORE_DIR before exporting a Replay dataset")

    repository = SQLAlchemyTaskRepository(database_url)
    try:
        repository.check_connection()
        repository.check_schema()
        task = repository.get(args.task_id)
        snapshots = repository.snapshots(args.task_id)
        cases = build_task_replay_cases(
            task,
            snapshots,
            LocalRawResponseObjectStore(raw_store_directory),
        )
        manifest = export_replay_dataset(
            cases,
            args.output_directory,
            dataset_id=args.dataset_id,
            dataset_version=args.dataset_version,
        )
    finally:
        repository.dispose()

    categories = Counter(entry.category for entry in manifest.entries)
    source_types = Counter(entry.original_source_type.value for entry in manifest.entries)
    print(
        json.dumps(
            {
                "dataset_id": manifest.dataset_id,
                "dataset_version": manifest.dataset_version,
                "classification": manifest.classification,
                "entries": len(manifest.entries),
                "categories": dict(sorted(categories.items())),
                "source_types": dict(sorted(source_types.items())),
                "redactions": sum(entry.redaction_count for entry in manifest.entries),
                "output_directory": str(args.output_directory),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
