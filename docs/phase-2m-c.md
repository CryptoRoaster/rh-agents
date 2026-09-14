# Phase 2M-C — Canonical risk input and durable authorization

One narrow server-side call takes a TradeCase that already has explicit sizing,
provable data and a real portfolio, runs it through the existing SENTINEL, and
binds the verdict to the whole basis it was reached from.

No fill, no launcher, no autonomous loop.

## Architecture decisions

**One transaction, in the established order.** Paper account, then trade case.
The portfolio a verdict was reached from and the binding that records it have to
commit together, or a crash between them leaves a decision bound to a state
nobody can reconstruct. `record_risk_decision_in_session` exists for exactly
that: the workflow service previously opened its own transaction, which made
atomicity impossible for a caller that already held a lock.

**Two digests, not one widened one.** `risk_input_digest` means the active
safety-critical evidence, and code, tests and stored rows already rest on that
meaning. Folding sizing, a portfolio and a limits set into it would redefine
every historical value silently — and would leave SIGNAL and FUSE out of risk
authority only by accident of what happens to be excluded today. The rest of the
basis is bound by a second digest, `risk_request_digest`, computed over the
recorded basis and stored beside it.

**One portfolio implementation.** The exposure, daily-loss and marks logic moved
out of `PaperTradingService` into `src/ledger/portfolio.py` and both paths now
use it. Two implementations of "what does the account hold, valued?" would
eventually disagree about money, and which was right would be decided by
whichever happened to run.

**The pause is read from the locked row.** Not through the port. The port's
presence says the stop is configured; its value is never consulted, because any
check-then-act on an unsynchronised snapshot leaves a window a concurrent pause
can commit inside. This is the same conclusion Phase 2L reached for intake.

**Refusals are not verdicts.** A rejection is terminal for the case and
permanently bars the market from opening another one. So every precondition —
incomplete data, a missing size, a source older than SENTINEL's own bound, an
unvaluable holding, a stop in force — refuses *before* SENTINEL is asked, and
writes nothing.

## Input mapping

Assembled server-side from the source that owns each fact. A caller supplies a
case id and a request key and nothing else: no `RiskInput`, price, size, limit,
portfolio, session or provider client.

- **price, token symbol and decimals** — the Phase 2M-A sizing assessment, which
  already checked asset identity, freshness and provenance.
- **liquidity depth** — the recorded market observation the sizing read.
- **routing** — ANCHOR established an executable route at a tested size, which is
  what `LiquiditySnapshot.routing` asks. It is not a claim about price.
- **tradability** — ATLAS `contract_integrity`. Contract integrity is exactly the
  token-level safety question: code present, supply known, no unreviewed proxy
  admin. Whether a route exists is a different question and travels on `routing`.
- **holder count and top-ten fraction** — the Phase 2M-B holder metrics, over
  total supply and unadjusted, which is the measure the limit is written against.
- **holder integrity** — ATLAS `holder_integrity`.
- **fee and slippage** — the configured PAPER assumptions, carried as
  assumptions. Never ANCHOR's execution deviation, a provider's price impact, a
  tested capacity or a measured fill cost.
- **portfolio** — the locked paper account and its positions, through the shared
  computation.
- **limits** — `RiskLimits`, obtained server-side and recorded in full with the
  request, because configuration can be edited and a binding that only named it
  could not be checked later.

`UNKNOWN` is deliberately absent from the verdict mapping: an unestablished
domain is a completeness gap the check already refuses on, never a value handed
to a risk evaluation. Nothing is invented and no `PASS` is assumed.

**Every nested observation keeps its own source instant.** SENTINEL checks each
of those ages independently, so an assembly time written into the built snapshot
would rejuvenate every source at once. Before calling, the service applies
SENTINEL's *actually configured* `max_snapshot_age_seconds` to each of them —
market, token, liquidity, holders — and refuses with
`SOURCE_OLDER_THAN_RISK_LIMIT` naming which. Nothing is loosened to make a
source fit. The completeness check's ninety seconds is the recorder's cadence;
thirty is SENTINEL's, and the tighter one decides.

A holding in another market cannot be valued — no mark source exists — so
`PORTFOLIO_MARKS_UNAVAILABLE` refuses rather than letting SENTINEL answer
`PORTFOLIO_DATA_UNKNOWN`, terminally, about a missing capability.

## Durable request identity

A new table, `trade_case_risk_requests` (migration `0007`).

The identity is **allocated once and persisted**, never derived on demand from
state that moves. Phase 2M-A recorded why the earlier proposal was insufficient:
an identity computed from live inputs cannot survive a crash and retry, because
the price will have moved by then and the retry would mint a different one —
losing the idempotency exactly when it was needed.

- `request_id = uuid5(request_key)`, and the `TradeIntent` id derives from it, so
  a retry finds the same object rather than building another.
- A row is written **only when SENTINEL actually ran**. A refusal records
  nothing, so an attempt that never reached a verdict cannot permanently consume
  the case's one request.
- `UniqueConstraint(trade_case_id)` is the contract: one canonical trade request
  per case. A second key is refused with `RISK_REQUEST_ALREADY_EXISTS`, and a
  changed data situation is not by itself permission for another one. **When a
  second request may legitimately be made is an open question this phase does
  not answer** — like resumption after execution, it needs its own contract.
- The same key replays: the stored verdict is returned, never recomputed.
- The full basis is persisted as JSON — sizing, readiness with every fact's
  provenance, cost assumptions, limits, portfolio, intent and the decision —
  because several of those are values their sources will not keep.

`expected_revision` and `expected_risk_input_digest` give a caller
submission-time checking, so a verdict cannot bind to a basis the case has left.

## Transaction boundaries and system stops

Locks: paper account (`FOR UPDATE`), then trade case. Time is read after both,
so waiting counts toward freshness and toward the UTC loss day. The case lock
means evidence cannot change mid-request — a concurrent supersession queues
behind rather than landing inside.

`PAUSE_SYSTEM` now takes effect in the case path. The rejection binding and
`paper_accounts.paused = True` commit in the same transaction, so no window
exists where the verdict stands and the pause does not. **Nothing here ever
clears it.** This closes the gap Phase 2L documented, where the TradeCase flow
could observe the durable pause but never set it.

An approval reserves no cash and authorises no fill. `authorizes_execution` and
`reserves_cash` are properties that return `False` whatever the outcome, stated
here rather than left for whoever writes the execution path to infer.

## Migration

`0007_trade_case_risk_requests`, one new table. Nothing existing is altered.

## Test evidence

38 tests in `tests/riskrequest/`, on a schema carrying accounting *and* workflow.
The production path runs throughout: the real workflow service against a real
database, the real completeness check, the real sizing calculation and
`src.risk.engine.evaluate` itself.

Honestly distinguished: the **evidence is fixture evidence**, and the market
feed's single read and the stop source are supplied as values. No specialist
worker runs in this suite — ATLAS's own suite proves the collector path — and no
launcher exists. A fixture run proves the integration, not the operation.

Covered: a complete case to a bound verdict with the whole basis; APPROVE,
LIMITED, REJECT and PAUSE_SYSTEM separated, with the pause landing atomically;
missing size, missing cost basis, sources older than SENTINEL's own bound, aged
holder metrics, an unvaluable holding, a not-ready case and a terminal case each
refusing without asking; blockers surviving a refusal; advisory-only changes
leaving the digest, the verdict and the blockers untouched; replay under the same
key and refusal under a different one; submission-time revision and digest
checks; a mid-request failure leaving nothing behind and a later request
succeeding.

Concurrency, PostgreSQL only: two callers with one key producing one evaluation
and one replay; two keys leaving one request standing; a pause racing a request
where neither can step over the other; evidence superseded during a request in
flight; two cases staying separate under one account lock; and a second case
reading the first's committed cash.

And the control: no `ExecutionRow`, no trade, no position and no cash movement
from a risk check.

## Remaining execution limits

- **No fill.** The durable case-to-order-to-fill link still does not exist. An
  approval is not a reservation, and a later fill must re-check under the lock.
- **When a second risk request is legitimate** is undecided, as is resumption
  after a terminal executed status.
- **No mark source** for holdings in other markets, so a portfolio with one
  refuses rather than being valued.
- **No launcher and no worker.** Nothing calls this service automatically; there
  is no public write API and no autonomous loop.
- `holder_count` remains provider-reported, and `RESPONSE_TIME` holder
  provenance is still accepted by the current ATLAS policy.
- Exits, live execution, signing and broadcast remain out of scope entirely.
