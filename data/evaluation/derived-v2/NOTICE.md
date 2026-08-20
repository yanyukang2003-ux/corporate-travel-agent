# Corporate Travel Derived Evaluation Dataset v2

This directory is a synthetic, derived, non-production evaluation artifact.

## Source attribution

- `UKPLab/PreferTripPlan`, revision
  `cdc7e11d5d2a0fd2cd53f03973819e469bc004b9`. Its dataset card declares
  Apache-2.0 and notes that inherited TravelPlanner material remains subject to
  upstream terms.
- `Alibaba-NLP/Open-Travel`, revision
  `b997227fb474eb578b7b951694fd4a6bf751bafa`, licensed CC BY-NC 4.0.

Every derived case retains the exact source record, source file hash, revision,
license label, and original query.

## V2 coverage and selection

- `workflow-cases.jsonl` contains 60 deterministic corporate-travel workflow
  fixtures derived from PreferTripPlan records.
- `intent-cases.jsonl` contains all 225 records from PreferTripPlan's recommended
  curated `test` split and all 250 records from Open-Travel's categorized `test`
  split. Five V1 Open-Travel training queries are retained as a separately
  labelled continuity cohort.
- The complete test splits are included without query- or model-performance
  filtering. `LEGACY_TARGETED` and `CURATED_FULL_SPLIT` are reported separately.
- PreferTripPlan structured facts and predicates provide the English intent gold.
  Open-Travel subtask files provide the Chinese task/scope gold. Source answers,
  hidden reasoning, tool trajectories, and inventory-like references are not used
  as expected answers.
- Missing-field and transport-preference metrics use explicit applicability flags;
  unlabelled dimensions are excluded rather than counted as empty correct answers.

## Use restrictions and synthetic boundaries

- All employee profiles, policies, meeting windows, inventory, approvals, and
  faults in workflow cases are synthetic.
- Every inventory fixture has `source_type=MOCK`; these records do not count
  toward real Provider snapshot coverage.
- Open-Travel-derived material is restricted to non-commercial evaluation unless
  separate permission is obtained.
