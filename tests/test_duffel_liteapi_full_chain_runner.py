from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from corporate_travel_agent.agent.orchestrator import TRANSIENT_RETRY_REASON
from corporate_travel_agent.domain.enums import ToolCallStatus
from corporate_travel_agent.domain.models import ToolCallRecord
from corporate_travel_agent.services.evaluation_full_chain import (
    duffel_read_sequence_valid,
    liteapi_read_sequence_valid,
    provider_retry_contract_valid,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET_PATH = PROJECT_ROOT / "evals/subsets/duffel-liteapi-real-full-chain-v2.json"


def test_full_chain_dataset_is_frozen_and_read_only() -> None:
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))

    assert dataset["status"] == "frozen"
    assert dataset["provider"] == "duffel+liteapi"
    assert dataset["provider_mode"] == "test+sandbox-read-only"
    assert dataset["records"] == 1
    assert dataset["base_external_calls"] == 4
    assert dataset["maximum_provider_attempts_per_operation"] == 3
    assert dataset["maximum_external_calls"] == 12
    assert dataset["maximum_duffel_calls"] == 6
    assert dataset["maximum_liteapi_calls"] == 6
    assert dataset["booking_enabled"] is False
    case = dataset["cases"][0]
    assert case["hotel_check_in"] is not None
    assert case["hotel_check_out"] is not None
    assert case["policy_currency"] == "USD"
    assert case["expected"]["allowed_revalidation_statuses"] == [
        "UNCHANGED",
        "PRICE_CHANGED",
    ]


def test_full_chain_request_sequences_allow_bounded_read_retries() -> None:
    flight_ref = "off_contract"
    duffel_search = {
        "provider": "duffel",
        "method": "POST",
        "path": "/air/offer_requests",
    }
    duffel_revalidate = {
        "provider": "duffel",
        "method": "GET",
        "path": f"/air/offers/{flight_ref}",
    }
    liteapi_rates = {
        "provider": "liteapi",
        "method": "POST",
        "path": "/hotels/rates",
    }

    assert duffel_read_sequence_valid(
        [duffel_search, duffel_search, duffel_revalidate],
        selected_flight_ref=flight_ref,
        max_attempts=3,
    )
    assert duffel_read_sequence_valid(
        [duffel_search, duffel_revalidate, duffel_revalidate, duffel_revalidate],
        selected_flight_ref=flight_ref,
        max_attempts=3,
    )
    assert liteapi_read_sequence_valid([liteapi_rates] * 6, max_attempts=3)


def test_full_chain_request_sequences_still_reject_over_budget_or_wrong_order() -> None:
    flight_ref = "off_contract"
    search = {"provider": "duffel", "method": "POST", "path": "/air/offer_requests"}
    revalidate = {
        "provider": "duffel",
        "method": "GET",
        "path": f"/air/offers/{flight_ref}",
    }
    liteapi_rates = {
        "provider": "liteapi",
        "method": "POST",
        "path": "/hotels/rates",
    }

    assert not duffel_read_sequence_valid(
        [revalidate, search],
        selected_flight_ref=flight_ref,
        max_attempts=3,
    )
    assert not duffel_read_sequence_valid(
        [search] * 4 + [revalidate],
        selected_flight_ref=flight_ref,
        max_attempts=3,
    )
    assert not liteapi_read_sequence_valid([liteapi_rates] * 7, max_attempts=3)


def test_full_chain_retry_contract_requires_a_linked_retryable_failure() -> None:
    started_at = datetime(2026, 8, 9, tzinfo=UTC)
    failed = ToolCallRecord(
        sequence=1,
        tool_name="provider.search_transport.outbound",
        tool_kind="PROVIDER",
        status=ToolCallStatus.FAILED,
        started_at=started_at,
        retryable=True,
    )
    recovered = ToolCallRecord(
        sequence=2,
        tool_name=failed.tool_name,
        tool_kind="PROVIDER",
        status=ToolCallStatus.SUCCEEDED,
        started_at=started_at,
        retry_of=failed.sequence,
        reason_code=TRANSIENT_RETRY_REASON,
    )

    assert provider_retry_contract_valid([failed, recovered], max_attempts=3)
    assert not provider_retry_contract_valid([failed, recovered], max_attempts=1)

    recovered.retry_of = None
    assert not provider_retry_contract_valid([failed, recovered], max_attempts=3)
