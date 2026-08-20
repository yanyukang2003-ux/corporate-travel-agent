# Stage 7 Baseline and Regression Evaluation

- Evaluation ID: `eval-39c67775-ef87-4920-8241-754280dc87e7`
- Mode: `baseline_initialization_self_check`
- Independent candidate run: `false`
- Absolute gate: **FAIL**
- Regression gate: **PASS**
- Release gate: **FAIL**
- Mutation detection: `100.0%`

The initialization comparison intentionally reuses the frozen source artifacts. It verifies
extraction, hashing, comparison and gate behavior; it is not evidence from a new Agent run.

## Bad-case feedback loop

- Dataset: `bad-case-regression` v1.0.0 (D6)
- Candidates: 7
- Accepted regression cases: 6
- Pending review: 1
- Accepted sources: 4 autonomous-recovery failures and 2 partial-result-disclosure failures
- Pending source: noncanonical Base64URL signature alias from the local authentication test
- Cases SHA-256: `83fcc1ca83ff35eeece6a884b22586c0d23ef46d910a178a1fbf9feaf5fcad34`
- Candidates SHA-256: `4bfc88dce9f195aa0477d47a3b012667cc37687d7dc99ee41db70d2988623b9c`

Every record carries an input hash, failure signature, trajectory-pattern hash and semantic
deduplication key. Only accepted, synthetic, pre-oracled cases are copied to `cases.jsonl`;
the pending authentication candidate remains outside release gates.

## Metric comparison

| Metric | Baseline | Current | Absolute | Regression |
|---|---:|---:|---|---|
| quality.task_pass_rate | 1.0 | 1.0 | pass | pass |
| quality.hard_assertion_pass_rate | 1.0 | 1.0 | pass | pass |
| quality.judge_quality_score | None | None | not_evaluated | not_evaluated |
| trajectory.trajectory_pass_rate | 1.0 | 1.0 | pass | pass |
| trajectory.tool_hallucination_call_rate | 0.0 | 0.0 | pass | pass |
| trajectory.parameter_hallucination_call_rate | 0.0 | 0.0 | pass | pass |
| trajectory.shadow_hallucination_claim_rate | 0.0 | 0.0 | pass | pass |
| efficiency.normal_duplicate_call_rate | 0.0 | 0.0 | pass | pass |
| efficiency.redundant_call_rate | 0.0 | 0.0 | pass | pass |
| efficiency.tool_calls_p95 | 5.0 | 5.0 | pass | pass |
| efficiency.tool_calls_max | 5.0 | 5.0 | pass | pass |
| recovery.recoverable_fault_success_rate | 0.3333333333333333 | 0.3333333333333333 | fail | pass |
| recovery.safe_degradation_rate | 1.0 | 1.0 | pass | pass |
| recovery.unsafe_recovery_rate | 0.0 | 0.0 | pass | pass |
| recovery.partial_result_disclosure_rate | 0.0 | 0.0 | fail | pass |
| stability.pass_power_3 | 1.0 | 1.0 | pass | pass |
| stability.mixed_run_rate | 0.0 | 0.0 | pass | pass |
| performance.total_latency_ms_p95 | 1.303 | 1.303 | not_evaluated | pass |
| cost.per_successful_task | None | None | not_evaluated | not_evaluated |
| stability.real_model_gate | None | None | not_evaluated | not_evaluated |

## Known absolute failures

recovery.recoverable_fault_success_rate, recovery.partial_result_disclosure_rate

## Required but unevaluated

quality.judge_quality_score, stability.real_model_gate

## Regressions

None

## Interpretation

No regression against the initialization snapshot does not mean the release gate passes.
Known Stage 5 recovery gaps remain absolute failures. Judge and real-model stability remain
unevaluated. Token and cost remain null because no LLM was exposed in the measured baseline.
