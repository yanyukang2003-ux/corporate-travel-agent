# Invalid run: sandbox network unavailable

This run did not receive any model response because the restricted sandbox could not
connect to the configured API endpoint (`APIConnectionError`). It has zero cost-ledger
rows and no token or cost measurements.

The `real_model_calls=144` field counts attempted invocations, not successful or
measured billable calls. The resulting `NEEDS_STRUCTURED_INPUT` states are fallback
behavior and must not be interpreted as a model-quality or stability result.

Use the valid replacement run instead:

`reports/evaluation-runs/d4-model-stability-deepseek-v102-20260809-rerun-1`
