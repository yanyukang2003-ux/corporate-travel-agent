from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from corporate_travel_agent.demo import build_demo_system, make_demo_request
from corporate_travel_agent.domain.enums import RawResponseAccessPolicy
from corporate_travel_agent.providers.base import TransportSearchQuery
from corporate_travel_agent.services.object_storage import (
    LocalRawResponseObjectStore,
    ObjectAccessDenied,
    ObjectConflictError,
    RawResponseReadContext,
    RawResponseReadPurpose,
)

FIXED_NOW = datetime(2026, 8, 1, 13, 45, tzinfo=UTC)


def test_local_store_is_write_once_and_enforces_read_context(tmp_path) -> None:
    store = LocalRawResponseObjectStore(tmp_path / "objects")
    content = b'{"provider":"mock","results":[]}'
    reference = store.put_bytes(
        object_key="mock/2026/08/01/transport/snapshot.json",
        content=content,
        content_type="application/json",
        stored_at=FIXED_NOW,
        retention_until=FIXED_NOW + timedelta(days=90),
        access_policy=RawResponseAccessPolicy.SYSTEM_REPLAY_OR_AUDIT_ADMIN,
    )

    with pytest.raises(ObjectAccessDenied):
        store.get_bytes(
            reference,
            context=RawResponseReadContext(
                actor_id="E1001",
                roles=frozenset({"employee"}),
                purpose=RawResponseReadPurpose.AUDIT,
            ),
        )

    restored = store.get_bytes(
        reference,
        context=RawResponseReadContext(
            actor_id="replay-worker",
            roles=frozenset({"system_replay"}),
            purpose=RawResponseReadPurpose.SYSTEM_REPLAY,
        ),
    )
    assert restored == content
    assert reference.sha256 == hashlib.sha256(content).hexdigest()
    assert reference.size_bytes == len(content)

    with pytest.raises(ObjectConflictError):
        store.put_bytes(
            object_key=reference.object_key,
            content=b'{"provider":"changed"}',
            content_type="application/json",
            stored_at=FIXED_NOW,
            retention_until=FIXED_NOW + timedelta(days=90),
            access_policy=RawResponseAccessPolicy.SYSTEM_REPLAY_OR_AUDIT_ADMIN,
        )


def test_mock_provider_archives_exact_raw_response(tmp_path) -> None:
    store = LocalRawResponseObjectStore(tmp_path / "objects")
    _, provider = build_demo_system(
        clock=lambda: FIXED_NOW,
        raw_response_store=store,
        raw_response_retention_days=30,
    )
    request = make_demo_request(task_id="raw-response-contract")
    snapshot = provider.search_transport(
        TransportSearchQuery(
            request.origin,
            request.destination,
            request.departure_after,
            request.arrive_by,
        )
    )

    assert snapshot.raw_response is not None
    raw = store.get_bytes(
        snapshot.raw_response,
        context=RawResponseReadContext(
            actor_id="audit-service",
            roles=frozenset({"audit_admin"}),
            purpose=RawResponseReadPurpose.AUDIT,
        ),
    )
    payload = json.loads(raw)
    assert payload["provider"] == "mock"
    assert payload["category"] == "transport"
    assert {item["ref_id"] for item in payload["results"]} == {
        "MU-EARLY",
        "MU-COMFORT",
        "G-BUFFER-FAIL",
    }
    assert snapshot.raw_payload_hash == snapshot.raw_response.sha256
    assert snapshot.raw_response.retention_until == FIXED_NOW + timedelta(days=30)
