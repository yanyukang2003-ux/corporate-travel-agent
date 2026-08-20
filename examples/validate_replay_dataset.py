from __future__ import annotations

import argparse
import json
from collections import Counter

from corporate_travel_agent.domain.enums import SourceType
from corporate_travel_agent.providers.replay import ReplayProvider
from corporate_travel_agent.services.replay_dataset import load_replay_dataset

REAL_SOURCE_TYPES = {SourceType.AUTHORIZED_API, SourceType.BROWSER_ASSISTED}
REAL_SNAPSHOT_TARGET = 20


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate hashes, redaction safety, schema, and query replay contracts."
    )
    parser.add_argument("dataset_directory")
    args = parser.parse_args()

    dataset = load_replay_dataset(args.dataset_directory)
    replay = ReplayProvider(list(dataset.snapshots))
    for entry in dataset.manifest.entries:
        snapshot = (
            replay.search_transport(entry.domain_query())
            if entry.category == "transport"
            else replay.search_hotels(entry.domain_query())
        )
        if snapshot.snapshot_id != entry.snapshot_id:
            raise RuntimeError(f"Replay mismatch for entry {entry.entry_id}")

    categories = Counter(entry.category for entry in dataset.manifest.entries)
    source_types = Counter(
        entry.original_source_type.value for entry in dataset.manifest.entries
    )
    real_entries = sum(
        1
        for entry in dataset.manifest.entries
        if entry.original_source_type in REAL_SOURCE_TYPES
    )
    print(
        json.dumps(
            {
                "valid": True,
                "dataset_id": dataset.manifest.dataset_id,
                "dataset_version": dataset.manifest.dataset_version,
                "manifest_sha256": dataset.manifest_sha256,
                "entries": len(dataset.manifest.entries),
                "categories": dict(sorted(categories.items())),
                "source_types": dict(sorted(source_types.items())),
                "redactions": sum(
                    entry.redaction_count for entry in dataset.manifest.entries
                ),
                "query_contracts_verified": len(dataset.manifest.entries),
                "real_snapshot_target": REAL_SNAPSHOT_TARGET,
                "real_snapshots_in_dataset": real_entries,
                "real_snapshots_remaining": max(
                    REAL_SNAPSHOT_TARGET - real_entries,
                    0,
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
