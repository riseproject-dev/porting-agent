---
# Shared contract for dispatchers with trusted, pre-staged work-queue plans.
---

The trusted preparation step stages one `submit` of the Work the queue does
not hold yet followed by one bounded `dispatch_next`, or, when the queue
already holds that Work, the bounded `dispatch_next` alone. Do not submit,
dispatch, edit intent files, reconstruct the plan, or call ordinary safe
outputs such as `noop` or `report_incomplete`. Read and explain the snapshot
only. A trusted step after the agent keeps only the intents the preparation
step staged and drops any other, and trusted processing verifies the batch
before admission and launch; an unassigned dispatcher has no worker Claim.
