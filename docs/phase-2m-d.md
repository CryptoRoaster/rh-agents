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

## Hardening round

Three defects, each reproduced against `4f7045d` before being fixed.

**The execution named a decision nobody made.** `recheck_decision_id` was
derived from the request key, so it pointed at nothing: the order had been built
on a real `RiskDecision` whose id was somewhere else entirely. `execute_in_session`
now returns a `PaperOutcome` carrying the decision and the order beside the fill,
and the case execution records the decision it was actually built on. After a
commit and a reload, `TradeCaseExecutionRow.recheck_decision_id`, `OrderRow.risk_id`
and `RiskRow.id` are one value, the intent, order and execution ids line up across
all four tables, and the decision itself is stored in the basis. Replay returns
the same references.

**Two settable limits sets could disagree.** `CaseFillService.limits` and
`PaperTradingService.limits` were configured separately, and the reproduction
ran a fill under the looser of the two while the stricter was written into the
basis as if it had applied — pre-checks refusing under one set while the
evaluation that actually gates the fill used another. The duplicate is removed:
`CaseFillService.limits` is a property reading the paper service's own object,
so there is nothing left that could diverge. A deliberately strict configuration
now refuses at the re-check and books nothing, and the basis records exactly the
limits the evaluation used. The account's durable pause is still folded in where
the evaluation happens, under the same lock.

**The fill landed outside the authorization it ran under.** *(First attempt
insufficient — see the follow-up below.)* The case binding and
workflow validity were checked at one instant; three persistence writes then
followed, and the order took a *fresh* clock read, which `OrderIntent` validates
only against the new SENTINEL window. With a controlled clock the original
five-second approval expired in that span while the re-check's own window was
still open, and the fill was booked at `12:00:06` against an authorization that
ended at `12:00:05`.

`execute_in_session` no longer reads the clock at all: the order is requested at
the instant every validity was checked at. Those intervening writes are this
transaction's own, and re-reading the clock between the checks and the fill
placed the execution outside windows that had been checked and found good
without anything having actually changed. Nothing is backdated and no deadline
is extended — the gap is removed rather than tolerated. A guard in the case-fill
service refuses and rolls back if the order instant ever differs from the checked
one, and a source-level assertion pins the absence of that clock read so a future
edit cannot quietly restore the drift.

An expiry reached *before* the decision instant still refuses, with no risk row,
no order and no position written — an expiry never becomes a terminal risk
verdict about the market. Historical replay of completed fills is unchanged.

### Follow-up: the wait itself

Removing the clock read did not remove the *wait*. Between `evaluate()` and the
executor the service persists the market, the intent and the decision, and each
of those is a database round trip that takes real time. Carrying the evaluation
instant onto the order recorded a moment that had already passed — an older
timestamp is an expiry bypass, not a fix for one.

Reproduced with a clock that advances when work happens rather than when it is
read, and a real persistence access made to take time: an approval ending at
`12:00:05` was filled while the clock stood at `12:00:16`, and the order
truthfully claimed `12:00:02`.

The clock is now read **at the execution boundary**, and every governing
validity is re-checked there on the inputs already loaded under the locks: the
service's own approval window, the original case binding, the central
evaluator's verdict, the completeness reading and every source age. The guard is
synchronous and nothing between it and the pure fill simulation touches the
database — a source-level assertion pins that, and `PaperExecutor.execute`
performs no I/O. The order and fill carry the boundary instant, which is the
instant the execution actually happened at.

An expiry is a typed stop: `EXECUTION_WINDOW_EXPIRED` for the case path,
`PaperExecutionExpired` for the standalone one. Both roll the whole transaction
back, so the decision, intent and market staged before the boundary do not
survive as a permanent record of an order that was never placed, and no
artificial final risk rejection is written in their place.

Two tests were corrected rather than kept: the one asserting that no clock read
occurs after the decision (there is one now, deliberately, at the boundary) and
the one that accepted a late fill as correct because "no time passed in the
span".

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
