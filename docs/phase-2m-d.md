# Phase 2M-D — Case-bound paper execution

One narrow server-side call fills a stored canonical risk request, once, and
binds the result durably to the case that authorised it.

No launcher, no autonomous loop, no public write API, no live executor, signing,
wallet or broadcast.

## Architecture decisions

**One transaction, in the established order.** Paper account, then trade case.
The fill, the ledger postings and the case's own record of them commit together
or not at all — a crash between them would leave a position nothing points at,
or a case that believes it traded when no money moved.

**The paper service is reused, not reimplemented.** `_process` was split into
`replay_in_session`, `positions_in_session` and `execute_in_session`; the
standalone path now wraps the last of these in its own transaction and behaves
exactly as before. The case-bound path calls the same method inside its own
transaction, so the risk evaluation, the executor and the accounting are one
implementation with two entry points rather than two implementations.

**The market builder is shared.** `risk_market` and `too_old_for` moved out of
the Phase 2M-C service into reusable functions, so the snapshot SENTINEL sees at
fill time is built by exactly the code that built the one it saw at approval
time — with a different identity key, because it is a different reading at a
different instant.

**Refusals are not failed fills.** Every precondition — an unapproved request,
an expired approval, changed safety evidence, an ineligible case, incomplete
data, a stale source, an unvaluable holding, a stop in force — refuses before
anything is written.

## Identity and replay

The stored `TradeCaseRiskRequestRow` is the origin. Its `basis["intent"]` is
loaded and used verbatim: no new intent id from the current revision, clock or
market price, and no re-derived quantity or notional. Re-deriving any of them
would make the order that is filled a different order from the one authorised.

`execution_results.intent_id` is already unique and the intent identity derives
from the stored request, so a second fill for one order is impossible by
construction. What was missing is the case reference — an execution row names no
case. Migration `0008` adds `trade_case_executions`, which is that join plus the
fill-time re-check: unique on `trade_case_id`, on `request_id`, and on each of
`intent_id`, `order_id`, `execution_id` and `recheck_decision_id`.

Replay is checked **before every authorization check**, deliberately. A
completed fill comes back exactly as recorded even once the approval behind it
has long expired: that is what happened, not a new permission. A verdict that
never became a fill comes back the same way — one order gets one decision, and a
refused fill is not retried into a yes.

A different call key never produces a second fill: the request is found by case,
and a key that does not match it is refused as a conflicting assignment rather
than answered.

## Authorization immediately before the fill

The caller names the stored request and, at most, the bindings it expects. It
supplies no quantity, price, `RiskInput`, limit or portfolio value.

For a new execution, all of the following, in this order:

- every stop — deployment kill switch, `RiskLimits.kill_switch`, trading mode,
  and the durable pause read from the account row this call already holds
  locked;
- the stored request is an `APPROVE` with authorization `APPROVED` —
  `RISK_LIMITED` lands in the same refusal, because a rejected size with a
  recorded capacity is still a rejected size;
- the case, recomputed by the central evaluator at the decision instant, is not
  terminal and is `RISK_APPROVED`;
- the current binding is the one the request was granted under, is `APPROVED`,
  has not expired, and covers the same safety digest the request recorded;
- the completeness check passes and has not aged out;
- every source is within SENTINEL's own configured `max_snapshot_age_seconds`;
- no holding exists that this system cannot value;
- and then `src.risk.engine.evaluate` runs again, on the portfolio and market as
  they are at that moment.

`RISK_LIMITED`, `REJECT` and `PAUSE_SYSTEM` produce no fill. There is no
downsize and no retry-until-pass. An existing approval reserves no cash — the
account is read under the lock at fill time, and the re-check may refuse on it.

Source times are untouched: each nested observation in the re-check's snapshot
keeps the instant its own source recorded. No limit is loosened to make anything
fit. All input reads happen before the last clock read, and everything from that
read to the verdict is synchronous, so one instant governs eligibility, the
approval's validity, every source age, the UTC loss day and SENTINEL itself.

The re-check and its outcome are bound to the execution without rewriting the
original request: the decision is persisted in `risk_decisions` (unique per
intent), the snapshot it judged is stored whole in the execution row's basis,
and `authorizing_binding_id` names the original approval unchanged.

## Transactions and system stops

Locks: paper account (`FOR UPDATE`), then trade case. Both the risk request and
the fill read the durable pause from the locked row rather than through the
port, so a stop cannot be lost by moving between the control plane, the risk
request and the fill. COMMANDER still has no setter, and nothing here clears a
pause.

A `PAUSE_SYSTEM` verdict at fill time sets `paper_accounts.paused` in the same
transaction as the rejection it came with, and no fill happens.

## Workflow completion

The workflow stays the status owner. A filled entry ends the case through
`complete_execution_in_session`, which uses the same guarded transition every
other status change uses — no direct status write. `EXECUTED` is reachable from
`RISK_APPROVED` and from nowhere else, so only an authorised case can end that
way, and it is terminal.

**An executed market does not open another case.** `MARKET_BARRING_CASE_STATUSES`
now holds `RISK_REJECTED` and `EXECUTED`, and intake refuses the second with
`POSITION_OPENED_FOR_MARKET`. A rejection is a verdict about the market; an
execution leaves a *position*, and adding to one, exiting one, or deciding that a
later entry is a different trade are contracts this system does not have.
Inventing a re-entry rule here would be a strategy hidden in a state machine.
`EXPIRED` and `CANCELLED` remain unbarred: both end a case without deciding
anything and without leaving anything behind.

## PAPER semantics

`PaperExecutor` performs the fill. Fees and slippage remain the configured
simulation assumptions from Phase 2M-B — no provider quote and no measured fill
cost is implied. `PaperFillRecorded.is_simulated` is a property that returns
`True` unconditionally, so the day a live path exists the thing that has to
change is visible.

Missing fresh marks for holdings in other markets remain an honest execution
stop. No valuation source and no zero valuation is invented.

## Migration

`0008_trade_case_executions`, one new table. Nothing existing is altered.
`TradeCaseStatus.EXECUTED` is a new enum value in an existing `String(40)`
column and needs no schema change.

## Test evidence

34 tests in `tests/casefill/`, on a schema carrying accounting *and* workflow.
The production path runs throughout: the real workflow service against a real
database, the real risk request, the real completeness check,
`src.risk.engine.evaluate`, `PaperExecutor` and the real ledger postings.

Honestly distinguished: the **evidence is fixture evidence**, and the market
feed's single read and the stop source are supplied as values. No specialist
worker runs in this suite and no launcher exists — an integration test is not
autonomous operation.

Covered: an approved request producing exactly one bound fill with correct
cash, fee, position and trade postings; the case-to-fill chain answerable in one
row; the re-check's own market snapshot stored and matching the decision's
fingerprint; the quantity taken from the stored request even when the price
moved; replay of a completed fill after the approval expired; a second call key
refused; `LIMITED`, `REJECT` and a fill-time `PAUSE_SYSTEM` producing no
execution; an expired approval, changed safety evidence and an expired case each
refusing; every stop; an unvaluable holding; a source past the risk limit; a
failure before commit leaving nothing behind and a later call succeeding; and the
completed case ending `EXECUTED` with intake refusing the market afterwards.

Concurrency, PostgreSQL only: two callers of one request producing one fill and
one replay; a racing second key adding nothing; two cases sharing cash where the
second is refused for `INSUFFICIENT_CASH` and the account never goes negative; a
pause racing a fill where neither steps over the other; and exactly one
completion transition under a race.

## Remaining limits

- **No exit.** Nothing sells, and `invalidation_price` and `target_prices` are
  still written by VECTOR and read by nothing.
- **No re-entry contract.** An executed market is barred from opening another
  case, deliberately, until one exists.
- **No mark source** for holdings in other markets, so a portfolio holding one
  refuses rather than being valued.
- **A fill-time refusal is final for that order.** One order gets one verdict;
  whether a later attempt should be possible is a question re-entry answers.
- **The approval window is short.** `RiskLimits.approval_ttl_seconds` defaults to
  five seconds, so a fill must follow its approval closely. That is the existing
  contract rather than something this phase chose.
- **No launcher and no worker.** Nothing calls this service automatically, and
  there is no public write API.
- Live execution, signing and broadcast remain out of scope entirely.
