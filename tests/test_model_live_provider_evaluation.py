from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from corporate_travel_agent.agent.ports import (
    IntentExtractionResult,
    LanguageModelError,
    LLMCallMetadata,
)
from corporate_travel_agent.agent.schemas import (
    IntentExtractionSchema,
    TripIntentFields,
)
from corporate_travel_agent.providers.duffel import DuffelProvider
from corporate_travel_agent.services.evaluation_model_live_provider import (
    load_model_live_provider_dataset,
    regrade_model_live_provider_evaluation,
    run_model_live_provider_evaluation,
)
from corporate_travel_agent.services.evaluation_trace import EvaluationTrace

PROJECT_ROOT = Path(__file__).parents[1]
DATASET_PATH = PROJECT_ROOT / "evals/subsets/model-duffel-workflow-smoke-v1.json"
DATASET_SHA256 = "2531cd4d64b4c75d3d76a730df6e02af9ec290fbd38e616a8b406e4becf9ea5e"
RECOVERY_DATASET_PATH = PROJECT_ROOT / "evals/subsets/model-duffel-workflow-recovery-v1.json"
FULL_RECOVERY_DATASET_PATH = (
    PROJECT_ROOT / "evals/subsets/model-duffel-workflow-full-recovery-v1.json"
)
DEEPSEEK_FULL_RECOVERY_DATASET_PATH = (
    PROJECT_ROOT / "evals/subsets/model-duffel-workflow-deepseek-full-recovery-v1.json"
)
DEEPSEEK_FULL_RECOVERY_DATASET_SHA256 = (
    "67e33420596a03f5201942bf899442683b53549f7122ecd6eefa2072ebbb1416"
)
PROVIDER_NOW = datetime(2026, 8, 3, tzinfo=UTC)


class AdvancingClock:
    """Deterministic shared clock that still creates distinct audit snapshots."""

    def __init__(self) -> None:
        self.current = PROVIDER_NOW

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(milliseconds=1)
        return value


class ExactIntentModel:
    prompt_version = "trip-intent-v2"
    model = "gpt-5.6"
    reasoning_effort = "medium"
    max_output_tokens = 1200

    def __init__(self) -> None:
        self.calls = 0

    def extract_trip_intent(self, message, *, task_id, traveler_id, context):
        del message, task_id, traveler_id, context
        self.calls += 1
        return IntentExtractionResult(
            payload=IntentExtractionSchema(
                classification="TRIP",
                fields=TripIntentFields(
                    origin="LHR",
                    destination="JFK",
                    departure_after=datetime(2026, 9, 15, tzinfo=UTC),
                    arrive_by=datetime(2026, 9, 17, tzinfo=UTC),
                    return_after=None,
                    return_before=None,
                    hotel_check_in=None,
                    hotel_check_out=None,
                    client_location=None,
                    hard_constraints=["flight_only"],
                    soft_preferences=[],
                ),
                provided_fields=[
                    "origin",
                    "destination",
                    "departure_after",
                    "arrive_by",
                    "hard_constraints",
                ],
                missing_required_fields=[],
                conflicts=[],
                assumptions=[],
                confidence=1.0,
                manipulation_detected=False,
            ),
            metadata=LLMCallMetadata(
                prompt_version=self.prompt_version,
                model="gpt-5.6-sol",
                requested_model=self.model,
                reasoning_effort=self.reasoning_effort,
                duration_ms=5,
                response_id=f"resp-{self.calls}",
                input_tokens=100,
                output_tokens=50,
                cached_input_tokens=10,
                cache_write_input_tokens=5,
                reasoning_output_tokens=20,
                total_tokens=150,
                service_tier="default",
            ),
        )

    def propose_search_adjustment(self, failure_facts, allowed_adjustments):
        del failure_facts, allowed_adjustments
        return None

    def explain_verified_options(self, options):
        return {item.option_id: "; ".join(item.explanation_facts) for item in options}


class FailFirstIntentModel(ExactIntentModel):
    def extract_trip_intent(self, message, *, task_id, traveler_id, context):
        if self.calls == 0:
            self.calls += 1
            raise LanguageModelError("Intent extraction failed: APIConnectionError")
        return super().extract_trip_intent(
            message,
            task_id=task_id,
            traveler_id=traveler_id,
            context=context,
        )


class ExactDeepSeekIntentModel(ExactIntentModel):
    prompt_version = "trip-intent-v3"
    model = "deepseek-v4-pro"

    def extract_trip_intent(self, message, *, task_id, traveler_id, context):
        result = super().extract_trip_intent(
            message,
            task_id=task_id,
            traveler_id=traveler_id,
            context=context,
        )
        return IntentExtractionResult(
            payload=result.payload,
            metadata=LLMCallMetadata(
                prompt_version=self.prompt_version,
                model=self.model,
                requested_model=self.model,
                reasoning_effort=self.reasoning_effort,
                duration_ms=5,
                response_id=f"deepseek-response-{self.calls}",
                input_tokens=100,
                output_tokens=50,
                cached_input_tokens=80,
                cache_write_input_tokens=None,
                reasoning_output_tokens=None,
                total_tokens=150,
                service_tier=None,
            ),
        )


class RecoverableFailFirstIntentModel(ExactIntentModel):
    def extract_trip_intent(self, message, *, task_id, traveler_id, context):
        if self.calls == 0:
            self.calls += 1
            raise LanguageModelError(
                "Intent extraction failed at OpenAI transport: APIConnectionError",
                error_code="OPENAI_API_CONNECTION_ERROR",
                layer="openai_transport",
                cause_type="APIConnectionError",
                cause_chain=("APIConnectionError", "ConnectError"),
                retryable=True,
                response_received=False,
            )
        return super().extract_trip_intent(
            message,
            task_id=task_id,
            traveler_id=traveler_id,
            context=context,
        )


class AlwaysRetryableIntentModel(ExactIntentModel):
    def extract_trip_intent(self, message, *, task_id, traveler_id, context):
        del message, task_id, traveler_id, context
        self.calls += 1
        raise LanguageModelError(
            "Intent extraction failed at OpenAI transport: APIConnectionError",
            error_code="OPENAI_API_CONNECTION_ERROR",
            layer="openai_transport",
            cause_type="APIConnectionError",
            cause_chain=(
                "APIConnectionError",
                "ConnectError",
                "SSLEOFError",
            ),
            retryable=True,
            response_received=False,
        )


def test_frozen_model_live_provider_dataset_and_artifacts_load() -> None:
    dataset, sha256, tools, price_table, price_sha256 = load_model_live_provider_dataset(
        DATASET_PATH, project_root=PROJECT_ROOT
    )

    assert dataset.records == 1
    assert dataset.attempts_per_case == 3
    assert sha256 == DATASET_SHA256
    assert "llm.extract_trip_intent" in tools
    assert "provider.search_transport.outbound" in tools
    assert price_table.price_table_version == ("openai-standard-gpt-5.6-detailed-20260803-v1")
    assert price_sha256 == dataset.price_table.sha256


def test_frozen_deepseek_duffel_dataset_and_artifacts_load() -> None:
    dataset, sha256, tools, price_table, price_sha256 = load_model_live_provider_dataset(
        DEEPSEEK_FULL_RECOVERY_DATASET_PATH,
        project_root=PROJECT_ROOT,
    )

    assert dataset.records == 1
    assert dataset.attempts_per_case == 3
    assert dataset.requested_model == "deepseek-v4-pro"
    assert dataset.usage_contract == "input_output_total"
    assert sha256 == DEEPSEEK_FULL_RECOVERY_DATASET_SHA256
    assert "llm.extract_trip_intent" in tools
    assert "provider.search_transport.outbound" in tools
    assert price_table.price_table_version == "model-prices-openai-20260802-v1"
    assert price_sha256 == dataset.price_table.sha256


def test_deepseek_basic_usage_contract_uses_conservative_cost_upper_bound(
    tmp_path: Path,
) -> None:
    model = ExactDeepSeekIntentModel()
    clock = AdvancingClock()

    def provider_factory(raw_store, currency):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == "/air/offer_requests"
            return httpx.Response(200, json=_duffel_search_payload())

        return DuffelProvider(
            "duffel_test_contract",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            expected_currency=currency,
            clock=clock,
            raw_response_store=raw_store,
        )

    output = tmp_path / "deepseek-live-provider"
    summary = run_model_live_provider_evaluation(
        dataset_path=DEEPSEEK_FULL_RECOVERY_DATASET_PATH,
        output_directory=output,
        project_root=PROJECT_ROOT,
        language_model=model,
        provider_factory=provider_factory,
        clock=clock,
    )

    assert model.calls == 3
    assert summary["runs"]["passed"] == 3
    assert summary["model"]["usage_contract"] == "input_output_total"
    assert summary["tokens"]["input_tokens"] == 300
    assert summary["tokens"]["output_tokens"] == 150
    assert summary["tokens"]["total_tokens"] == 450
    assert summary["tokens"]["cached_input_tokens"] == 240
    assert summary["tokens"]["cache_write_input_tokens"] is None
    assert summary["tokens"]["reasoning_output_tokens"] is None
    assert summary["estimated_cost_usd"] == 0.000261
    assert all(summary["gates"].values())


def test_three_attempt_model_live_provider_runner_is_offline_testable(
    tmp_path: Path,
) -> None:
    model = ExactIntentModel()
    clock = AdvancingClock()

    def provider_factory(raw_store, currency):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url.path == "/air/offer_requests"
            body = json.loads(request.content)
            assert body["data"]["slices"] == [
                {
                    "origin": "LHR",
                    "destination": "JFK",
                    "departure_date": "2026-09-15",
                }
            ]
            return httpx.Response(200, json=_duffel_search_payload())

        return DuffelProvider(
            "duffel_test_contract",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            expected_currency=currency,
            clock=clock,
            raw_response_store=raw_store,
        )

    output = tmp_path / "model-live-provider"
    summary = run_model_live_provider_evaluation(
        dataset_path=DATASET_PATH,
        output_directory=output,
        project_root=PROJECT_ROOT,
        language_model=model,
        provider_factory=provider_factory,
        clock=clock,
    )

    assert model.calls == 3
    assert summary["runs"]["passed"] == 3
    assert summary["runs"]["pass_power_3"] == 1.0
    assert summary["runs"]["intent_consistency_rate"] == 1.0
    assert summary["runs"]["trajectory_consistency_rate"] == 1.0
    assert summary["calls"] == {
        "model": 3,
        "provider": 3,
        "model_cap": 3,
        "provider_cap": 3,
    }
    assert all(summary["gates"].values())
    assert summary["hallucination"]["parameter_hallucination"]["failed_attempts"] == 0
    assert summary["tokens"]["cached_input_tokens"] == 30
    assert summary["tokens"]["cache_write_input_tokens"] == 15
    assert summary["tokens"]["reasoning_output_tokens"] == 60
    assert summary["estimated_cost_usd"] > 0

    traces = [
        EvaluationTrace.model_validate_json(line)
        for line in (output / "traces.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(traces) == 3
    assert all(trace.fingerprint.actual_model == "gpt-5.6-sol" for trace in traces)
    assert all(trace.evaluation_mode == "model_live_provider" for trace in traces)
    raw_files = tuple(
        path
        for path in (output / "raw-provider-responses").rglob("*.json")
        if not path.name.endswith(".metadata.json")
    )
    assert len(raw_files) == 3
    assert "duffel_test_" not in (output / "run-summary.json").read_text()


def test_infrastructure_failure_is_not_misclassified_as_hallucination(
    tmp_path: Path,
) -> None:
    model = FailFirstIntentModel()
    clock = AdvancingClock()

    def provider_factory(raw_store, currency):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_duffel_search_payload())

        return DuffelProvider(
            "duffel_test_contract",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            expected_currency=currency,
            clock=clock,
            raw_response_store=raw_store,
        )

    output = tmp_path / "fail-first-live-provider"
    summary = run_model_live_provider_evaluation(
        dataset_path=DATASET_PATH,
        output_directory=output,
        project_root=PROJECT_ROOT,
        language_model=model,
        provider_factory=provider_factory,
        clock=clock,
    )

    assert summary["runs"]["passed"] == 2
    assert summary["hallucination"]["parameter_hallucination"] == {
        "model_exposure": "measured",
        "evaluable_attempts": 2,
        "not_evaluable_attempts": 1,
        "failed_attempt_rate": 0.0,
        "failed_attempts": 0,
    }
    assert summary["hallucination"]["shadow_hallucination"]["failed_attempts"] == 0
    assert summary["operational_reliability"]["failures_by_category"] == {
        "infrastructure_connection_error": 1
    }
    assert summary["resource_accounting"]["accounted_attempts"] == 2
    assert summary["resource_accounting"]["completed_response_accounting_coverage"] == 1.0
    assert summary["gates"]["resource_accounting_gate"] is False

    regraded = regrade_model_live_provider_evaluation(
        dataset_path=DATASET_PATH,
        output_directory=output,
        project_root=PROJECT_ROOT,
    )
    assert regraded["hallucination"] == summary["hallucination"]
    assert (output / "run-summary.regraded.json").is_file()
    assert (output / "evaluation-report.regraded.md").is_file()


def test_recovery_dataset_records_and_recovers_one_llm_transport_failure(
    tmp_path: Path,
) -> None:
    model = RecoverableFailFirstIntentModel()
    clock = AdvancingClock()

    def provider_factory(raw_store, currency):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_duffel_search_payload())

        return DuffelProvider(
            "duffel_test_contract",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            expected_currency=currency,
            clock=clock,
            raw_response_store=raw_store,
        )

    output = tmp_path / "recovered-live-provider"
    summary = run_model_live_provider_evaluation(
        dataset_path=RECOVERY_DATASET_PATH,
        output_directory=output,
        project_root=PROJECT_ROOT,
        language_model=model,
        provider_factory=provider_factory,
        clock=clock,
        retry_sleep=lambda _: None,
    )

    assert model.calls == 4
    assert summary["runs"]["passed"] == 3
    assert summary["calls"] == {
        "model": 4,
        "provider": 3,
        "model_cap": 6,
        "provider_cap": 3,
    }
    assert summary["retries"] == {
        "maximum_llm_attempts_per_run": 2,
        "external_model_request_cap": 6,
        "failed_model_requests": 1,
        "retry_attempts": 1,
        "recovered_retries": 1,
        "error_layers": {"openai_transport": 1},
        "terminal_error_causes": {"ConnectError": 1},
        "maximum_provider_attempts_per_run": 1,
        "external_provider_request_cap": 3,
        "failed_provider_requests": 0,
        "provider_retry_attempts": 0,
        "recovered_provider_retries": 0,
        "provider_error_layers": {},
        "provider_terminal_error_causes": {},
        "common_terminal_error_causes": [],
    }
    assert summary["gates"]["integration_gate"] is True
    assert summary["gates"]["three_run_stability_gate"] is False
    assert summary["gates"]["resource_accounting_gate"] is False
    first = summary["attempt_results"][0]
    assert first["checks"]["retry_contract_valid"] is True
    assert first["llm_error_diagnostics"] == (
        {
            "error_type": "APIConnectionError",
            "error_code": "OPENAI_API_CONNECTION_ERROR",
            "error_layer": "openai_transport",
            "cause_type": "APIConnectionError",
            "cause_chain": ("APIConnectionError", "ConnectError"),
            "response_received": False,
            "http_status": None,
            "request_id": None,
            "retryable": True,
        },
    )


def test_full_recovery_dataset_recovers_one_duffel_connection_failure(
    tmp_path: Path,
) -> None:
    model = ExactIntentModel()
    clock = AdvancingClock()
    provider_requests = 0

    def provider_factory(raw_store, currency):
        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal provider_requests
            provider_requests += 1
            if provider_requests == 1:
                raise httpx.ConnectError("sensitive network details", request=request)
            return httpx.Response(200, json=_duffel_search_payload())

        return DuffelProvider(
            "duffel_test_contract",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            expected_currency=currency,
            clock=clock,
            raw_response_store=raw_store,
        )

    output = tmp_path / "full-recovery-live-provider"
    summary = run_model_live_provider_evaluation(
        dataset_path=FULL_RECOVERY_DATASET_PATH,
        output_directory=output,
        project_root=PROJECT_ROOT,
        language_model=model,
        provider_factory=provider_factory,
        clock=clock,
        retry_sleep=lambda _: None,
    )

    assert model.calls == 3
    assert provider_requests == 4
    assert summary["runs"]["passed"] == 3
    assert summary["calls"] == {
        "model": 3,
        "provider": 4,
        "model_cap": 6,
        "provider_cap": 6,
    }
    assert summary["retries"]["failed_provider_requests"] == 1
    assert summary["retries"]["provider_retry_attempts"] == 1
    assert summary["retries"]["recovered_provider_retries"] == 1
    assert summary["retries"]["provider_error_layers"] == {"duffel_transport": 1}
    assert summary["gates"]["integration_gate"] is True
    assert summary["gates"]["three_run_stability_gate"] is False
    assert summary["gates"]["resource_accounting_gate"] is True
    assert summary["tool_efficiency"]["duplicate_calls"] == 1
    assert summary["tool_efficiency"]["authorized_retry_calls"] == 1
    assert summary["tool_efficiency"]["unjustified_duplicate_calls"] == 0
    first = summary["attempt_results"][0]
    assert first["checks"]["provider_retry_contract_valid"] is True
    assert first["provider_error_diagnostics"] == (
        {
            "error_type": "ConnectError",
            "error_code": "DUFFEL_TRANSPORT_CONNECTION_ERROR",
            "error_layer": "duffel_transport",
            "cause_type": "ConnectError",
            "cause_chain": ("ConnectError",),
            "response_received": False,
            "http_status": None,
            "request_id": None,
            "retryable": True,
        },
    )


def test_exhausted_retry_is_policy_compliant_and_fully_traced(
    tmp_path: Path,
) -> None:
    model = AlwaysRetryableIntentModel()
    clock = AdvancingClock()

    def provider_factory(raw_store, currency):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_duffel_search_payload())

        return DuffelProvider(
            "duffel_test_contract",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            expected_currency=currency,
            clock=clock,
            raw_response_store=raw_store,
        )

    output = tmp_path / "exhausted-live-provider"
    summary = run_model_live_provider_evaluation(
        dataset_path=FULL_RECOVERY_DATASET_PATH,
        output_directory=output,
        project_root=PROJECT_ROOT,
        language_model=model,
        provider_factory=provider_factory,
        clock=clock,
        retry_sleep=lambda _: None,
    )

    assert model.calls == 6
    assert summary["runs"]["passed"] == 0
    assert all(item["checks"]["retry_contract_valid"] for item in summary["attempt_results"])
    assert all(
        item["checks"]["provider_retry_contract_valid"] for item in summary["attempt_results"]
    )
    assert summary["retries"]["terminal_error_causes"] == {"SSLEOFError": 6}
    assert summary["retries"]["common_terminal_error_causes"] == []
    assert summary["resource_accounting"]["outbound_requests"] == 6
    assert summary["resource_accounting"]["traced_outbound_requests"] == 6
    assert summary["resource_accounting"]["outbound_request_trace_coverage"] == 1.0
    assert summary["resource_accounting"]["failed_before_response_requests"] == 6
    assert summary["tool_efficiency"]["duplicate_calls"] == 3
    assert summary["tool_efficiency"]["authorized_retry_calls"] == 3
    assert summary["tool_efficiency"]["unjustified_duplicate_calls"] == 0

    regraded = regrade_model_live_provider_evaluation(
        dataset_path=FULL_RECOVERY_DATASET_PATH,
        output_directory=output,
        project_root=PROJECT_ROOT,
    )
    assert all(item["checks"]["retry_contract_valid"] for item in regraded["attempt_results"])


def _duffel_search_payload() -> dict[str, object]:
    return {
        "data": {
            "live_mode": False,
            "offers": [
                {
                    "id": "off_contract_lhr_jfk",
                    "live_mode": False,
                    "total_amount": "221.28",
                    "total_currency": "USD",
                    "expires_at": "2026-08-05T06:57:24Z",
                    "slices": [
                        {
                            "segments": [
                                {
                                    "departing_at": "2026-09-15T10:00:00",
                                    "arriving_at": "2026-09-15T13:00:00",
                                    "origin": {"time_zone": "Europe/London"},
                                    "destination": {"time_zone": "America/New_York"},
                                }
                            ]
                        }
                    ],
                }
            ],
        }
    }
