# GPT-5.6 Preflight Quality Smoke Review

## Scope

This review interprets the two rule-quality smoke results from the real-model
qualification preflight. It does not add API calls and is not a formal quality or
stability gate.

## Results

| Case | Rule result | Evidence |
|---|---:|---|
| `prefer-intent-0001` | fail | Expected `MULTI_DAY_TRIP`; observed `NEEDS_CLARIFICATION`. Expected missing fields `arrive_by`, `return_after`, `return_before`; observed `arrive_by`, `return_after`. |
| `open-train-missing-fields-0085` | pass | Classification, exact missing fields, clarification behavior, unsupported-constraint rejection, provider-call safety, and inventory-hallucination checks all matched. |

Both model calls returned valid Pydantic Structured Outputs. Both tasks avoided
premature Provider calls and inventory hallucination.

## Interpretation

The system prompt lists both task-type labels such as `MULTI_DAY_TRIP` and the
state-like label `NEEDS_CLARIFICATION`, but does not define which one wins when a
multi-day request is recognizable and still lacks required time windows. The frozen D2
labels use both conventions across different cases. Therefore the failed case is a
pending Prompt/gold-contract review, not yet a confirmed model defect.

The source query also contains fixed 2025 dates while the frozen evaluation reference
time is in 2026. Dataset aging is a second possible confounder and should be reviewed
before this case becomes a release-gating regression.

## Bad-case disposition

- Failure signature: `4736e14cf9b83885eac3052e92507fe71d62d6f22a517e2c74432d8c7d8c00dd`
- Review status: `pending`
- Automatic acceptance into D6: prohibited until the expected classification and
  date-aging policy are confirmed by the project owner.

