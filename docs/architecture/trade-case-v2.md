# TRADE_CASE_V2: SENTIMENT becomes advisory

Under `trade-case-v1` SENTIMENT (SIGNAL) is a required pre-trigger input: a case
without at least five Farcaster casts from three authors naming the contract
address in six hours stays `EVIDENCE_PENDING` for good, because SIGNAL runs once
per case and is never re-armed. For young tokens that made social popularity a
hidden entry condition.

## The one difference

`TRADE_CASE_V2` (`src/orchestration/workflow/policy.py`) is `TRADE_CASE_V1` with
the SENTIMENT requirement `required=False` (it was never safety-critical) and the
SIGNAL task slot marked optional. Everything else is identical: ORBIT/DISCOVERY,
ATLAS/ONCHAIN, VECTOR/TRADE_SETUP, PULSE/TRIGGER and ANCHOR/LIQUIDITY_EXECUTION
stay required, the safety table and risk digest are unchanged, refreshable
sources are unchanged, and FUSE stays optional.

SIGNAL still runs when enabled, and its evidence stays honest and audited:
UNKNOWN stays UNKNOWN (with `TOO_FEW_OBSERVATIONS`, `TOO_FEW_AUTHORS`, …), a
provider failure stays a failure and records no evidence, AVAILABLE readings are
stored and readable, and a NEGATIVE reading is not a veto. SENTIMENT stays
outside the risk digest, so it gains no risk authority directly or indirectly.

## Versions per case

Every case is evaluated under the `workflow_version` it was opened with
(`policy_for`): the evaluator, task creation, evidence binding, derived-task
re-arming, source refresh, risk readiness, the commander view and FUSE all
resolve the case's own policy. New cases open as `trade-case-v2`
(`CURRENT_WORKFLOW`); existing `trade-case-v1` cases keep V1 rules for life, even
when a V2 service reads them. An unknown stored version fails closed with
`UNSUPPORTED_WORKFLOW_VERSION`.

No migration: `trade_cases.workflow_version` is `String(40)` without a value
constraint, and no existing row is rewritten.

The PAPER preflight follows the current workflow: with SIGNAL off (and no social
key) a run is not blocked; SIGNAL is shown as not enabled.
