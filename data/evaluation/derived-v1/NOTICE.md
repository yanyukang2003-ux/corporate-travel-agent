# Corporate Travel Derived Evaluation Dataset v1

This directory is a synthetic, derived, non-production evaluation artifact.

## Source attribution

- `UKPLab/PreferTripPlan`, revision
  `cdc7e11d5d2a0fd2cd53f03973819e469bc004b9`. The dataset card declares
  Apache-2.0 and states that downstream TravelPlanner data retains its upstream
  licence terms.
- `Alibaba-NLP/Open-Travel`, revision
  `b997227fb474eb578b7b951694fd4a6bf751bafa`, licensed CC BY-NC 4.0.

The exact source record, source file hash, revision, licence label, and original
query are retained in every derived case.

## Permitted role in this project

- `workflow-cases.jsonl` contains 60 deterministic corporate-travel workflow
  fixtures derived from PreferTripPlan records.
- `intent-cases.jsonl` contains 30 query-only Chinese intent fixtures selected
  from Open-Travel.
- All employee profiles, policies, meeting windows, inventory, approvals, and
  faults are synthetic.
- Every inventory fixture has `source_type=MOCK`.
- These records do not count toward the 20-real-Provider-snapshot target.
- Open-Travel-derived material is restricted to non-commercial evaluation unless
  separate permission is obtained.

Open-Travel model answers, hidden reasoning, and tool trajectories are not used
as expected answers. The project evaluates deterministic states and invariants,
not similarity to generated prose.
