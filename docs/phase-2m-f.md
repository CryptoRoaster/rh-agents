# Phase 2M-F — Explicit paper exit

One narrow server-side call closes one open, case-bound PAPER position
completely. The caller names the position and an idempotent key, and supplies
nothing else.

No automatic stop-loss or take-profit. No partial sales, no re-entry, no
launcher, no autonomous loop, no public write API. No live trading, signing,
broadcast or Docker.

## Contracts audited first

Read before anything was written, and reused rather than reimplemented:

| Contract | What it already did | What this phase does with it |
| --- | --- | --- |
| `src.risk.engine.evaluate` | full SELL handling, including `INSUFFICIENT_POSITION` | asked again, unchanged, for the exit |
| `PaperExecutor.execute` | `side == SELL` already prices *below* quote by the slippage | reused as the only fill simulation |
| `apply_fill` | SELL path: proportional basis release, realised P&L, cash in | reused as the only accounting |
| `execute_in_session` | lock-aware evaluation, boundary clock read, PAUSE_SYSTEM, loss-day roll | reused as the only execution path |
| `portfolio_state` / `portfolio_basis` | 2M-E valuation and its recomputable record | reused unchanged |
| `risk_market` / `too_old_for` | SENTINEL's market view and its own freshness bound | reused unchanged |
| `TradeCaseService` | status ownership, guarded transitions | left alone entirely |

Nothing in the risk engine, the executor or the ledger was modified. The only
change outside the new module is additive: `PaperOutcome` now carries the
`Trade` and the updated `Position` the ledger produced, so a caller can record a
realised result without computing a second one beside the accounting.

## Exit and identity contract

**The caller names a position id and a request key.** No quantity, price, mark,
limit or portfolio value crosses the boundary.

**The exit sells the whole open holding.** The quantity is read from the
position row *under the account lock* and fixed into the order built from it. A
chosen size would be a strategy this system does not have, and a residue would
be a position nobody decided to keep.

**Provenance must be unambiguous.** The holding is matched to exactly one
case-bound PAPER entry — same chain, same network, same base asset — and the
market the position records must be the market that entry happened in, pair,
chain, network and provider. Zero matches is `POSITION_ORIGIN_UNKNOWN`, more
than one is `POSITION_ORIGIN_AMBIGUOUS`, a position with no recorded market is
`POSITION_MARKET_UNKNOWN`, and a disagreement between the two records is
`POSITION_MARKET_MISMATCH`. Selling something this system cannot say it bought
would be a trade with no origin; attributing the sale to whichever case
mentions the asset would be worse, because the binding is durable and a wrong
one is a false record rather than a missing one.

**One order per key.** `intent_id = uuid5("rh-agents:paper-exit-intent:<key>")`.
A retry finds the same order rather than minting a second. A key that later names
a different position, or an intent whose stored content differs, is
`EXIT_KEY_MISMATCH` — a conflicting reuse, never a second sale.

**No short, no oversale, no partial.** The quantity is the holding; SENTINEL's
`INSUFFICIENT_POSITION` is the backstop; a holding at zero is
`POSITION_ALREADY_CLOSED`, which is not a failed sale but no sale at all.

## SELL risk rules

The decision table SENTINEL already implements, read out of `src/risk/engine.py`
before anything was built:

| Check | BUY | SELL |
| --- | --- | --- |
| `KILL_SWITCH` → PAUSE_SYSTEM | ✓ | ✓ |
| `MODE_NOT_EXECUTABLE`, `ASSET_MISMATCH` | ✓ | ✓ |
| `STALE_OR_FUTURE_MARKET`, `STALE_OR_FUTURE_SAFETY_DATA` | ✓ | ✓ |
| `INVALID_OR_STALE_INTENT_TIMING` | ✓ | ✓ |
| `TOKEN_*`, `ROUTING_*`, `HOLDERS_*`, `ACCOUNTING_*` | ✓ | ✓ |
| `HOLDER_METRICS_UNKNOWN`, `HOLDER_CONCENTRATION_LIMIT` | ✓ | ✓ |
| `LIQUIDITY_UNKNOWN`, `INSUFFICIENT_LIQUIDITY` | ✓ | ✓ |
| `SLIPPAGE_UNKNOWN`, `SLIPPAGE_LIMIT` | ✓ | ✓ |
| `FEES_UNKNOWN`, `INVALID_FEES` | ✓ | ✓ |
| `PORTFOLIO_DATA_UNKNOWN` | ✓ | ✓ |
| `DAILY_LOSS_LIMIT` → PAUSE_SYSTEM | ✓ | ✓ |
| `INSUFFICIENT_CASH`, `MAX_EXPOSURE`, `MAX_POSITION_SIZE` | ✓ | — |
| `max_additional_notional_usd` (buy capacity) | ✓ | — |
| `INSUFFICIENT_POSITION` | — | ✓ |

So the entry-side capacity checks already do not apply to a sale, and the sale
already has its own guard. **The minimal explicit exit contract is therefore: a
SELL intent for the whole holding, evaluated by the existing engine at its own
instant, with its decision persisted and referenced.** Nothing was added to the
engine, no limit was loosened, and no outcome is overridden.

The consequence is stated rather than engineered away: the checks that also gate
a purchase gate the sale too. A token that has become illiquid, or concentrated,
or whose market data has aged out, cannot be sold here. A position far enough
under water trips `DAILY_LOSS_LIMIT` on its *unrealised* loss and pauses the
system instead of being sold. That is the existing contract, and **an emergency
exit with special rights is deliberately not in this phase**.

## System stops

`OBSERVE`, a configured kill switch, an unreadable stop source and the durable
account pause all refuse the exit, checked in that order before any data is
read. The pause is read from the account row this transaction already holds
locked, never through the port — the same conclusion Phase 2L reached for
intake. A `PAUSE_SYSTEM` verdict at exit time sets `paper_accounts.paused` in
the same transaction as the rejection it came with, and nothing here ever clears
it.

## Transaction and replay

Locks: **paper account (`FOR UPDATE`), then trade case** — the established
order, unchanged, and no third lock is introduced.

The SELL fill, the cash and fee movements, the position, the realised result and
the exit's own durable record commit together or roll back together.

Replay is checked **before every authorization check and before any valuation**,
on the intent identity derived from the key. A completed sale comes back exactly
as recorded even once the approval behind it has long expired — that is what
happened, not a new permission — and a stored refusal comes back the same way.
Neither touches the market layer, proved against a feed that raises on contact.

Two callers cannot sell one holding twice: the account lock orders them, the
second finds nothing held, and `trade_case_exits` is unique on the position, the
entry fill and the case as a backstop. A refused sale is not retried into a yes.

## Accounting

`apply_fill` releases the whole cost basis, books the realised result, credits
cash net of fees and adds any realised loss to the day — on the UTC day that
`roll_loss_day` established inside the same evaluation. The exit record takes
`realized_pnl_usd` straight from the `Trade` the ledger booked and derives
`cost_basis_released_usd` as the difference between the position before and
after. Neither is recomputed: two implementations of "what did this sale
produce?" would disagree about money on the day it mattered.

## Time boundary

Every input — provenance, readiness, evidence, the market snapshot, the
positions — is loaded before the authoritative clock read. From there to the
verdict is synchronous.

At the execution boundary the clock is read again, truthfully, and the decision's
own approval window plus every source it rested on are re-checked there: the
completeness reading, each mark used, and the market snapshot's own ages.
Nothing between that check and the pure fill simulation touches the database.
An expiry is `EXECUTION_WINDOW_EXPIRED` with the reason named, rolls the whole
started execution back, and never becomes a risk rejection.

## Audit

The exit row stores the entry references, the market judged whole, the limits,
the cost assumptions, the intent, the decision, the fill, the booked trade, the
position before and after, `portfolio_basis(state)` and the `RiskContext` handed
to SENTINEL. `replay_portfolio_basis` recomputes that context through the same
`portfolio_state`, from the record alone.

## Workflow and re-entry

**No new workflow status, deliberately.** The entry case stays `EXECUTED`, which
is terminal; its request, binding and execution are not rewritten. A transition
out of a terminal status would have to be invented, and the only thing it could
mean — "this market is available again" — is precisely the re-entry contract
that does not exist. `MARKET_BARRING_CASE_STATUSES` still holds `EXECUTED`, so
intake still refuses the market with `POSITION_OPENED_FOR_MARKET` after a
successful exit. The exit is recorded beside the case instead, and the central
workflow remains the only status owner: nothing here calls a forced transition.

Because `EXECUTED` is terminal it cannot lapse between being read and being
relied on, so the status read under the case lock is the whole eligibility
question and no second workflow opinion is formed.

## Migration

`0010_trade_case_exits` — one new table, additive. Nothing existing is altered
and no status is rewritten. The uniqueness is the contract: one exit per
position, per entry fill, per case and per key, plus unique intent, order,
execution and decision ids. Head is `0010`.

## Test evidence

31 tests in `tests/paperexit/`, on a schema carrying accounting *and* workflow.
The production path runs throughout: the real workflow service against a real
database, the real risk request, the real case fill, the real completeness
check, `src.risk.engine.evaluate`, `PaperExecutor` and the real ledger postings.
Every entry is a real case-bound fill, not a fixture position.

Honestly distinguished: the **evidence is fixture evidence**, and the market
feed's read and the stop source are supplied as values — two ports, as values.
**No provider is called for real anywhere in this suite**, no specialist worker
runs and no launcher exists. The controlled clock advances when work happens;
**no sleep is used for any time case**.

Covered: an entry closed completely with quantity nil, the cost basis released
and cash and fees booked; gain and loss both, with the realised result computed
independently outside the code and compared against the exit record, the booked
trade, the position and the day's loss; no oversale and no negative quantity;
the same order replaying unchanged against an unreachable market layer three
hours later; a stored refusal replaying the same way; a key naming another
position refused; a holding with no case-bound entry, no recorded market or a
mismatched market each refused with nothing written; every stop including
`OBSERVE`, the kill switch, an unreadable stop and the durable pause; a fill-time
`PAUSE_SYSTEM` stored atomically with its rejection and no sale; stale and
missing market data as typed stops; an unvaluable other holding as a typed stop;
a strict limit refusing the sale with no special rights.

Concurrency, PostgreSQL only: two identical orders producing one sale and one
replay; two different keys selling the holding at most once; a pause racing an
exit where neither steps over the other; and a failure before commit leaving no
partial bookings, with a later order still succeeding.

Time and audit: an expiry during persistence rolling the whole sale back, both
for the decision's own window and for a mark that aged out at the boundary; the
stored basis recomputing the judged `RiskContext` after the account and the
position rows have been changed underneath it; a loss realised after UTC
midnight landing on the new day with the old day's loss rolled off, and the
control case on the same day accumulating; and a closed position still barred
from opening another case and from being sold again.

## Remaining limits

- **No partial exit.** An exit closes the position or does not happen.
- **No emergency exit.** Every check that gates a purchase still gates the sale,
  including the daily-loss stop that a deeply under-water position trips on its
  unrealised loss.
- **No stop-loss or take-profit.** `invalidation_price` and `target_prices` are
  still written by VECTOR and read by nothing. Nothing triggers an exit
  automatically.
- **No re-entry.** A closed position is not a permit, and the market stays
  barred.
- **One exit per position, per entry and per case**, by construction.
- **No launcher and no worker.** Nothing calls this service automatically, and
  there is no public write API.
- Live execution, signing and broadcast remain out of scope entirely.
