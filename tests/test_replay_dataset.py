from __future__ import annotations

import json
import stat
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from corporate_travel_agent.demo import build_demo_system, make_demo_request
from corporate_travel_agent.providers.replay import ReplayProvider
from corporate_travel_agent.services.object_storage import InMemoryRawResponseObjectStore
from corporate_travel_agent.services.redaction import REDACTED_VALUE, redact_json
from corporate_travel_agent.services.replay_dataset import (
    ReplayDatasetError,
    ReplayExportCase,
    build_task_replay_cases,
    export_replay_dataset,
    load_replay_dataset,
    load_replay_library,
)

FIXED_NOW = datetime(2026, 8, 1, 13, 45, tzinfo=UTC)


def _export_dataset(tmp_path):
    store = InMemoryRawResponseObjectStore()
    workflow, _ = build_demo_system(
        clock=lambda: FIXED_NOW,
        raw_response_store=store,
    )
    task = workflow.create_task(make_demo_request(task_id="replay-dataset-source"))
    cases = build_task_replay_cases(
        task,
        workflow.tasks.snapshots(task.task_id),
        store,
    )
    output = tmp_path / "dataset"
    manifest = export_replay_dataset(
        cases,
        output,
        dataset_id="mock-contract-smoke",
        dataset_version="1",
        created_at=FIXED_NOW,
    )
    return output, manifest, cases


def test_redactor_removes_keyed_and_inline_secrets_without_mutating_source() -> None:
    safe_sha256 = "123456789012345678901234567890123456789012345678901234567890abcd"
    source = {
        "passenger_name": "Alice Example",
        "nested": {
            "note": "Contact alice@example.com or 13800138000",
            "link": "https://provider.example/search?token=secret#fragment",
            "sha256": safe_sha256,
        },
    }

    result = redact_json(source)

    assert source["passenger_name"] == "Alice Example"
    assert result.payload["passenger_name"] == REDACTED_VALUE
    assert "alice@example.com" not in result.payload["nested"]["note"]
    assert "13800138000" not in result.payload["nested"]["note"]
    assert result.payload["nested"]["link"] == "https://provider.example/search"
    assert result.payload["nested"]["sha256"] == safe_sha256
    assert result.redaction_count == 4


def test_exported_dataset_loads_and_replays_every_bound_query(tmp_path) -> None:
    output, manifest, _ = _export_dataset(tmp_path)
    loaded = load_replay_dataset(output)
    replay = ReplayProvider(list(loaded.snapshots))

    assert manifest.classification == "INTERNAL_REDACTED"
    assert len(loaded.snapshots) == 3
    assert len(loaded.manifest_sha256) == 64
    for entry in loaded.manifest.entries:
        snapshot = (
            replay.search_transport(entry.domain_query())
            if entry.category == "transport"
            else replay.search_hotels(entry.domain_query())
        )
        assert snapshot.snapshot_id == entry.snapshot_id
        assert snapshot.raw_response is None
        assert snapshot.query_hash == entry.query_hash
        assert stat.S_IMODE((output / entry.snapshot_file).stat().st_mode) == 0o600
        assert stat.S_IMODE((output / entry.redacted_raw_file).stat().st_mode) == 0o600


def test_loader_rejects_tampered_snapshot_file(tmp_path) -> None:
    output, manifest, _ = _export_dataset(tmp_path)
    snapshot_path = output / manifest.entries[0].snapshot_file
    snapshot_path.write_bytes(snapshot_path.read_bytes() + b" ")

    with pytest.raises(ReplayDatasetError, match="snapshot hash mismatch"):
        load_replay_dataset(output)


def test_loader_rejects_path_traversal_in_manifest(tmp_path) -> None:
    output, _, _ = _export_dataset(tmp_path)
    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["entries"][0]["snapshot_file"] = "../outside.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ReplayDatasetError, match="Unsafe dataset path"):
        load_replay_dataset(output)


def test_export_rejects_sensitive_values_in_normalized_snapshot(tmp_path) -> None:
    _, _, cases = _export_dataset(tmp_path)
    case = cases[0]
    unsafe_snapshot = replace(case.snapshot, provider="ops@example.com")
    unsafe_case = ReplayExportCase(
        category=case.category,
        query=case.query,
        snapshot=unsafe_snapshot,
        raw_response=case.raw_response,
    )

    with pytest.raises(ReplayDatasetError, match="still contains sensitive data"):
        export_replay_dataset(
            (unsafe_case,),
            tmp_path / "unsafe-dataset",
            dataset_id="unsafe-dataset",
            dataset_version="1",
            created_at=FIXED_NOW,
        )


def test_export_refuses_to_overwrite_an_existing_dataset(tmp_path) -> None:
    output, _, cases = _export_dataset(tmp_path)

    with pytest.raises(ReplayDatasetError, match="Refusing to overwrite"):
        export_replay_dataset(
            cases,
            output,
            dataset_id="mock-contract-smoke",
            dataset_version="2",
            created_at=FIXED_NOW,
        )


def test_replay_provider_rejects_duplicate_query_hashes() -> None:
    workflow, provider = build_demo_system(clock=lambda: FIXED_NOW)
    request = make_demo_request(task_id="duplicate-replay-query")
    workflow.create_task(request)
    snapshots = workflow.tasks.snapshots(request.task_id)
    snapshot = snapshots[0]

    with pytest.raises(ValueError, match="unique query hashes"):
        ReplayProvider([snapshot, snapshot])


def test_library_rejects_duplicate_source_snapshots(tmp_path) -> None:
    _, _, cases = _export_dataset(tmp_path)
    export_replay_dataset(
        cases,
        tmp_path / "library" / "first",
        dataset_id="first",
        dataset_version="1",
        created_at=FIXED_NOW,
    )
    export_replay_dataset(
        cases,
        tmp_path / "library" / "second",
        dataset_id="second",
        dataset_version="1",
        created_at=FIXED_NOW,
    )

    with pytest.raises(ReplayDatasetError, match="duplicate source snapshots"):
        load_replay_library(tmp_path / "library")
