Read-only audit complete. No files changed. Overall verdict: deterministic workflow boundaries are strong, but production readiness is blocked by a false-positive safety gate, incomplete model-output provenance, retry-policy drift, and absent production monitoring.

## Findings

### Critical

1. D4 safety/provenance assertions are hard-coded to pass.

- Evidence: assertions require zero unsupported parameter values and zero untrusted mutations in `examples/build_agent_eval_v1.py:1733-1739,1764-1772`, but the model observation sets both to literal zero in `src/corporate_travel_agent/services/evaluation_agent_eval.py:1304-1317`. Current reports consequently claim 24/24 and 100% assertions at `reports/evaluation-runs/d4-model-smoke-deepseek-20260809-final-rerun-3/agent-eval-v1-run-summary.json:8-20` and 24×3 at `reports/evaluation-runs/d4-model-stability-deepseek-v102-20260809-rerun-1/agent-eval-v1-run-summary.json:8-20`.
- Failure/impact: a compromised model can change a route or constraint and the safety/provenance assertions still pass. The 24/24 result is valid for measured workflow assertions, but not evidence that parameter provenance or prompt-injection containment passed.
- Remediation: derive unsupported values and mutations from persistent per-slot lineage, user turns, safe defaults, and task/tool deltas. Return `not_evaluable` where no real measurement exists.
- Verification: mutation-test each counter; a scripted model emitting a valid-schema but ungrounded destination/constraint must fail the release gate.

### High

2. `provided_fields` is model-authored and treated as proof of grounding.

- Evidence: the schema lets the model choose `provided_fields` at `src/corporate_travel_agent/agent/schemas.py:49-60`. The merge uses user text only for hotel detection, then accepts other listed values directly at `src/corporate_travel_agent/agent/intent_calibration.py:284-337`; L2 validates shape/business consistency, not textual support, at `src/corporate_travel_agent/agent/param_extraction_loop.py:415-499`.
- Failure/impact: prompt injection or ordinary hallucination can emit a plausible but unsupported city/date, pass validation, and cause searches, policy evaluation, and a wrong handoff.
- Remediation: require slot-level evidence records such as `{source_turn, evidence_span, derivation, prior_value}`; deterministically validate aliases and derived dates, otherwise clarify.
- Verification: compromised-model tests should emit valid-schema ungrounded cities/dates and assert `NEEDS_CLARIFICATION`, zero provider calls, and a nonzero unsupported-value metric.

3. LLM retry handling misclassifies 429/5xx and differs between production and evaluation.

- Evidence: every HTTP status is marked non-retryable at `src/corporate_travel_agent/agent/openai_adapter.py:315-325`. Production creates the SDK client with defaults at `openai_adapter.py:59-67` and uses one orchestrator LLM attempt via `src/corporate_travel_agent/demo.py:42-49`; model evals disable SDK retries at `examples/run_agent_eval_model_smoke.py:99-102` and configure two explicit attempts at `src/corporate_travel_agent/services/evaluation_agent_eval.py:1473-1483`. The installed OpenAI SDK is 2.52.0 with two default internal retries.
- Failure/impact: production retries may be hidden inside one tool record, while an exhausted 429/503 immediately becomes terminal to the orchestrator. Eval retry behavior is not production-equivalent.
- Remediation: disable SDK retries everywhere; centrally classify 408/409/429/5xx and transport errors, honor bounded `Retry-After`, and expose the same retry settings in production and evals.
- Verification: fake 429 with `Retry-After`, 503-then-success, connection failure, and 400; assert exact linked tool records, delay, cost, and no retry for 400. Direct audit probe confirmed both 429 and 503 currently return `retryable=False`.

4. Multi-turn replacement semantics are heuristic and provenance is reset each turn.

- Evidence: full-route replacement depends on a narrow phrase regex at `src/corporate_travel_agent/agent/orchestrator.py:98-107`; stale hotel/time/constraints are cleared only when it matches at `orchestrator.py:281-296`. `CalibrationTrace` is recreated for every submitted turn at `orchestrator.py:359-364`, so prior slot provenance is not loaded.
- Failure/impact: “Actually make it Shanghai to Guangzhou” does not trigger replacement, so route-scoped hotel dates or meeting constraints can survive from the old trip. The audit probe confirmed this phrase returns `False`.
- Remediation: add explicit `revision_mode`/slot operations (`supplement`, `replace_route`, `replace_times`, `clear`) and persist provenance with turn number and supersession links.
- Verification: paraphrase/property tests for route changes, time corrections, explicit clearing, and mixed-language revisions; assert no stale slots and complete provenance after every turn.

5. Production has no model monitoring, and the available trace recorder retains free text.

- Evidence: the API builds the workflow without a `trace_observer` at `src/corporate_travel_agent/api/main.py:146-156`; events are discarded when it is absent at `src/corporate_travel_agent/agent/orchestrator.py:1873-1875`. The observability implementation explicitly supports only offline backfill with zero production records at `src/corporate_travel_agent/services/evaluation_observability.py:104-140,395-399`. The recorder stores redacted input directly at `src/corporate_travel_agent/services/evaluation_trace.py:164-205`, while redaction does not treat `message`/arbitrary text as sensitive at `src/corporate_travel_agent/services/redaction.py:100-129`. Raw prompts are visible in `reports/evaluation-runs/phase13-model-duffel-recovery-20260803/traces.jsonl:1`.
- Failure/impact: production cannot detect model drift, retry spikes, OOS spikes, token/cost changes, or safety events. Wiring the existing recorder directly into production would violate the documented hash-only free-text policy.
- Remediation: implement a production observer that stores prompt/model/version, state/tool/error metrics, hashes, tokens, latency, and cost—never raw free text in the ordinary telemetry stream.
- Verification: API integration test must emit monitoring events and alerts while a canary secret and arbitrary user sentence are absent from every telemetry artifact.

6. Failed billed model responses disappear from cost accounting, and there is no dedicated LLM budget.

- Evidence: Responses and Chat output are validated before usage metadata is returned at `src/corporate_travel_agent/agent/openai_adapter.py:135-153,180-219`. Successful metadata alone is appended at `src/corporate_travel_agent/agent/orchestrator.py:509-515`; failure traces at `orchestrator.py:1622-1648` carry no tokens. Eval counts all LLM tool records at `src/corporate_travel_agent/services/evaluation_agent_eval.py:896-923`, but the ledger writes only successful metadata rows at `evaluation_agent_eval.py:1819-1842`. The orchestrator has only a shared tool count and per-call retry count at `orchestrator.py:130-165`.
- Failure/impact: a billed invalid JSON/refusal/parse failure can produce `real_model_calls > ledger_rows`, understate tokens/cost, and still yield a numeric partial total without a completeness status. Repeated repair/rechat calls can also consume all 12 shared calls before provider work.
- Remediation: attach response usage to `LanguageModelError`, ledger every attempted call, mark totals complete/lower-bound/unavailable, and add per-task LLM-call/token/USD caps plus reserved provider capacity.
- Verification: fake an invalid JSON response containing usage; it must produce a failed ledger row and counted cost. Test LLM-budget exhaustion separately from provider capacity.

### Medium

7. LLM exhaustion records `CLARIFY` but forces structured input.

- Evidence: taxonomy explicitly recommends clarification at `src/corporate_travel_agent/agent/error_recovery.py:154-173`; the orchestrator always transitions to `NEEDS_STRUCTURED_INPUT` at `src/corporate_travel_agent/agent/orchestrator.py:410-437`. The current expectation is encoded in `tests/test_intent_scenarios.py:524-536`.
- Failure/impact: a temporary model/parse failure sends the user to a form instead of asking them to retry or restate, increasing abandonment.
- Remediation: transition exhausted retryable/parse failures to `NEEDS_CLARIFICATION` with a user-facing question; reserve structured input for repeated conversational failure or explicit user choice.
- Verification: injected transport exhaustion and parse failure should yield a clarification question, accept the next message, and only later escalate to structured input.

8. `OUT_OF_SCOPE` cannot reopen.

- Evidence: `submit_message` excludes OOS at `src/corporate_travel_agent/agent/orchestrator.py:238-247`; OOS has no outgoing transition at `src/corporate_travel_agent/workflow/state_machine.py:64-65`; the sequential eval whitelist also excludes it at `src/corporate_travel_agent/services/evaluation_agent_eval.py:855-866`.
- Failure/impact: one misclassification, or a user switching from an unsupported request to a valid trip, permanently bricks the task.
- Remediation: only accept OOS when no usable trip slots exist, and allow a new user message to reset/reopen into `DRAFT` with explicit carry/reset semantics.
- Verification: first model response OOS, second turn valid trip; assert a second LLM call and normal planning. Also test valid trip slots plus OOS classification are clarified rather than terminated.

9. Provider capability selection is coupled to model-name substrings.

- Evidence: API mode is inferred from whether the model name contains `deepseek`/`flash` at `src/corporate_travel_agent/agent/openai_adapter.py:49-58`; API configuration exposes only the model at `src/corporate_travel_agent/api/main.py:70-75`. Chat mode uses prompt-embedded schema plus `json_object`, not native strict schema enforcement, at `openai_adapter.py:156-199`. The adapter contract test covers only Responses at `tests/test_intent_scenarios.py:553-607`.
- Failure/impact: a neutral alias on a Chat-only OpenAI-compatible endpoint selects Responses and fails; provider schema behavior can change without a regression test.
- Remediation: use an explicit provider profile/capability configuration (`api_mode`, structured-output mode, thinking controls, token parameter names), with startup validation.
- Verification: contract matrix for OpenAI Responses, DeepSeek Chat, aliases, invalid JSON, refusal, empty choices, and usage shapes.

10. No fairness/bias evaluation is present.

- Evidence: D4 records only language composition at `data/evaluation/agent-eval-v1/manifest.json:35-38`; quality slices are scenario-only at `src/corporate_travel_agent/services/evaluation_quality.py:288-334`; adversarial slices cover category/vector/language only at `src/corporate_travel_agent/services/evaluation_adversarial.py:821-843`.
- Failure/impact: differential clarification/OOS/error rates across language variants, employee levels, departments, or accessibility-related phrasing can ship unnoticed.
- Remediation: add privacy-preserving counterfactual pairs and report per-slice task pass, unsupported-value, clarification, and OOS gaps with minimum sample sizes.
- Verification: paired equivalent prompts varying only the selected attribute; gate on maximum error-rate disparity and investigate statistically meaningful gaps.

No separate Low finding.

## Handoff next-item assessment

- LLM failure should clarify rather than force structured input: **confirmed not implemented**.
- Transient vs 429 handling: **confirmed misaligned**; direct probe returned `OPENAI_HTTP_429 False` and `OPENAI_HTTP_503 False`.
- OOS reopening: **confirmed not implemented**; OOS is terminal in both workflow and sequential eval driving.

## Strengths

- The LLM port is narrow and cannot choose tools, approve policy, or create inventory: `src/corporate_travel_agent/agent/ports.py:117-132`.
- Structured outputs are strict Pydantic models with `extra="forbid"`: `src/corporate_travel_agent/agent/schemas.py:32-60`; the Responses path uses native typed parsing at `openai_adapter.py:119-140`.
- The parameter loop has bounded repair, two-layer validation, deterministic defaults, and city anti-fabrication intent.
- Tool calls are bounded, traced, retry-linked, and protected by side-effect classification; external writes are excluded from blind retry.
- Evaluation assets are frozen and fingerprinted, with multilingual/risk/adversarial composition and 24×3 stability runs.

Verification run: four targeted recovery/multi-turn tests passed with cache and bytecode writes disabled.