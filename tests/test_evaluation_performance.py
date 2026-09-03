from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from corporate_travel_agent.evaluation.performance import (
    ModelPriceTable,
)

DATASET_ROOT = Path(__file__).parents[1] / "data" / "evaluation" / "derived-v2"
PRICE_TABLE = Path(__file__).parents[1] / "evals" / "pricing" / "model-prices-unconfigured-v1.json"
MODEL_SUBSET = Path(__file__).parents[1] / "evals" / "subsets" / "intent-model-smoke-v1.json"
MODEL_PREFLIGHT_SUBSET = (
    Path(__file__).parents[1] / "evals" / "subsets" / "intent-model-preflight-v1.json"
)


def test_price_table_cannot_hide_unknown_prices_as_zero() -> None:
    payload = json.loads(PRICE_TABLE.read_text())
    assert payload["models"] == {}
    table = ModelPriceTable.model_validate(payload)
    assert table.status == "unconfigured"

    payload["status"] = "configured"
    with pytest.raises(ValidationError, match="must contain at least one model"):
        ModelPriceTable.model_validate(payload)


