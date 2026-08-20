# Original formal run invalid

The external Duffel Test Mode transaction completed and its Order was confirmed
cancelled. The original runner then failed while validating an unregistered
evaluation-mode enum, before writing its model/workflow trace and formal result.

`recovery-result.json` validates the seven archived Duffel responses and cleanup
state without making another model or provider call. It does not reconstruct the
missing model usage or workflow trace, so this directory must not be claimed as a
fully passing formal evaluation.
