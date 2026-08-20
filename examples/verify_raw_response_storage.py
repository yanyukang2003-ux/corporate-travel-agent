from __future__ import annotations

import hashlib
import json

from fastapi.testclient import TestClient

from corporate_travel_agent.api.main import app, raw_response_store, workflow
from corporate_travel_agent.services.object_storage import (
    RawResponseReadContext,
    RawResponseReadPurpose,
)


def main() -> None:
    if raw_response_store.backend_name != "local-worm":
        raise SystemExit("Set RAW_RESPONSE_STORE_DIR before running this smoke test")

    client = TestClient(app)
    created = client.post(
        "/trip-tasks",
        json={
            "traveler_id": "E1001",
            "origin": "Beijing",
            "destination": "Shanghai",
            "departure_after": "2026-08-05T05:00:00+08:00",
            "arrive_by": "2026-08-06T10:00:00+08:00",
            "return_after": "2026-08-06T12:00:00+08:00",
            "return_before": "2026-08-06T18:00:00+08:00",
            "hotel_check_in": "2026-08-05",
            "hotel_check_out": "2026-08-06",
        },
    )
    created.raise_for_status()
    task_id = created.json()["task_id"]
    snapshots = workflow.tasks.snapshots(task_id)
    if not snapshots or any(item.raw_response is None for item in snapshots):
        raise RuntimeError("Provider raw responses were not archived")

    total_bytes = 0
    for snapshot in snapshots:
        reference = snapshot.raw_response
        if reference is None:
            raise RuntimeError("Snapshot is missing its raw-response reference")
        content = raw_response_store.get_bytes(
            reference,
            context=RawResponseReadContext(
                actor_id="raw-response-smoke",
                roles=frozenset({"system_replay"}),
                purpose=RawResponseReadPurpose.SYSTEM_REPLAY,
            ),
        )
        if hashlib.sha256(content).hexdigest() != snapshot.raw_payload_hash:
            raise RuntimeError("Archived provider response failed integrity validation")
        total_bytes += len(content)

    public_snapshots = client.get(
        f"/trip-tasks/{task_id}/inventory-snapshots"
    ).json()
    if any("object_key" in item["raw_response"] for item in public_snapshots):
        raise RuntimeError("Public API leaked an internal object key")

    print(
        json.dumps(
            {
                "task_id": task_id,
                "task_state": created.json()["state"],
                "store_backend": raw_response_store.backend_name,
                "snapshot_count": len(snapshots),
                "archived_bytes": total_bytes,
                "integrity_verified": True,
                "object_keys_hidden_from_api": True,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
