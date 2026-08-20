from __future__ import annotations

import argparse
import json
from collections import Counter

from corporate_travel_agent.providers.replay import ReplayProvider
from corporate_travel_agent.services.replay_dataset import load_replay_library

REAL_SNAPSHOT_TARGET = 20


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate every dataset in a Replay library and aggregate coverage."
    )
    parser.add_argument("library_directory", nargs="?", default="data/replay-datasets")
    args = parser.parse_args()

    library = load_replay_library(args.library_directory)
    source_types: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    verified = 0
    for dataset in library.datasets:
        replay = ReplayProvider(list(dataset.snapshots))
        for entry in dataset.manifest.entries:
            snapshot = (
                replay.search_transport(entry.domain_query())
                if entry.category == "transport"
                else replay.search_hotels(entry.domain_query())
            )
            if snapshot.snapshot_id != entry.snapshot_id:
                raise RuntimeError(f"Replay mismatch for entry {entry.entry_id}")
            source_types[entry.original_source_type.value] += 1
            categories[entry.category] += 1
            verified += 1

    print(
        json.dumps(
            {
                "valid": True,
                "datasets": len(library.datasets),
                "unique_snapshots": library.entry_count,
                "query_contracts_verified": verified,
                "categories": dict(sorted(categories.items())),
                "source_types": dict(sorted(source_types.items())),
                "real_snapshot_target": REAL_SNAPSHOT_TARGET,
                "real_snapshots_collected": library.real_snapshot_count,
                "real_snapshots_remaining": max(
                    REAL_SNAPSHOT_TARGET - library.real_snapshot_count,
                    0,
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
