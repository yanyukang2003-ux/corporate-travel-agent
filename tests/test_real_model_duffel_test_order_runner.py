from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from corporate_travel_agent.services.evaluation_performance import load_model_price_table

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "examples/run_real_model_duffel_test_order.py"
DATASET_PATH = PROJECT_ROOT / "evals/subsets/model-duffel-test-order-e2e-v1.json"
SPEC = importlib.util.spec_from_file_location("real_model_duffel_test_order", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


def _dataset() -> dict:
    return json.loads(DATASET_PATH.read_text(encoding="utf-8"))


def test_d14_dataset_is_frozen_test_only_and_bounded() -> None:
    dataset = _dataset()

    RUNNER._validate_dataset(dataset)
    assert dataset["status"] == "frozen"
    assert dataset["provider_mode"] == "test"
    assert dataset["attempts_per_case"] == 1
    assert dataset["maximum_model_calls"] == 1
    assert dataset["maximum_total_duffel_http_calls"] == 7
    assert dataset["live_booking_enabled"] is False
    assert dataset["test_order_enabled"] is True
    assert dataset["test_order_cancellation_required"] is True
    assert dataset["cases"][0]["synthetic_passenger"]["email"].endswith("@example.com")


def test_dynamic_duffel_refs_normalize_to_frozen_http_contract() -> None:
    assert RUNNER._normalize_paths(
        [
            ("POST", "/air/offer_requests"),
            ("GET", "/air/offers/off_dynamic"),
            ("POST", "/air/orders"),
            ("GET", "/air/orders/ord_dynamic"),
            ("POST", "/air/order_cancellations"),
            ("POST", "/air/order_cancellations/ore_dynamic/actions/confirm"),
            ("GET", "/air/orders/ord_dynamic"),
        ]
    ) == [
        "POST /air/offer_requests",
        "GET /air/offers/{offer_id}",
        "POST /air/orders",
        "GET /air/orders/{order_id}",
        "POST /air/order_cancellations",
        "POST /air/order_cancellations/{cancellation_id}/actions/confirm",
        "GET /air/orders/{order_id}",
    ]


def test_frozen_intent_and_conservative_model_cost_contract() -> None:
    expected = _dataset()["cases"][0]["expected_intent"]
    request = SimpleNamespace(
        origin="LHR",
        destination="JFK",
        departure_after=RUNNER.datetime.fromisoformat(expected["departure_after"]),
        arrive_by=RUNNER.datetime.fromisoformat(expected["arrive_by"]),
        return_after=None,
        return_before=None,
        hotel_check_in=None,
        hotel_check_out=None,
        hard_constraints=("flight_only",),
        soft_preferences=(),
    )
    table, _ = load_model_price_table(
        PROJECT_ROOT / "evals/pricing/model-prices-openai-20260802-v1.json"
    )

    assert RUNNER._intent_matches(request, expected) is True
    assert (
        RUNNER._estimated_cost(
            {
                "input_tokens": 1000,
                "output_tokens": 100,
                "total_tokens": 1100,
                "cached_input_tokens": 500,
                "cache_write_input_tokens": None,
                "reasoning_output_tokens": None,
            },
            "deepseek-v4-pro",
            "deepseek-v4-pro",
            table,
        )
        == 0.000522
    )


def test_synthetic_passenger_factory_preserves_explicit_safety_marker() -> None:
    passenger = RUNNER._synthetic_passenger(_dataset()["cases"][0]["synthetic_passenger"])

    assert passenger.synthetic is True
    assert passenger.email.endswith("@example.com")
