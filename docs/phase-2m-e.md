# Phase 2M-E — Paper position marks

A server-side source of current valuation data for every open PAPER position, so
the risk request and the case-bound fill can account for holdings in markets
other than the one they are running in.

No exits, no re-entry, no topping up. No launcher, no autonomous loop, no public
write API. No live trading, signing, broadcast or Docker. No trading strategy and
no new risk limits.

## The identity contract

**An asset is not a market.** A position named only an `asset_id`, and an asset
trades in many pools, on many chains, through many providers. Valuing it means
choosing one of them, and choosing silently — in the one place where guessing
wrong misprices the whole portfolio — is exactly what this phase refuses to do.

So a position now records the market it was acquired in: `market_pair_id`,
`market_chain`, `market_network`, `market_provider` — the same four fields
`MarketIdentity` already uses, so the mark is looked up by the identity the
recorder wrote, not by a reconstruction of it.

The fields are **nullable**, and that is the contract rather than an oversight.
Anything acquired before this was recorded has no market on file, and there is no
answer that can be inferred for it. Absence is reported as
`POSITION_MARKET_UNKNOWN` and refuses; it never falls back to "the market this
case happens to be in".

A fill carries the identity forward unchanged (`apply_fill`): a fill changes a
position's size, never the market it lives in. A newly opened position is stamped
with the market the entry was evaluated in.

## The data source

`src/orchestration/valuation/` — `PositionValuationReader` over a
`ValuationMarketInput` port with exactly one method, `latest(identity)`. That is
the **existing** recorded-market reader the risk request already uses; no second
provider, no new feed and no new client. Historical data, on-chain reads and
pricing heuristics are all absent, deliberately.

For each non-zero holding the reader produces either a `PositionMark` or a typed
`ValuationRefusal`:

| Refusal | Meaning |
| --- | --- |
| `POSITION_MARKET_UNKNOWN` | the position records no market |
| `MARKET_NOT_RECORDED` | nothing has been recorded for that market |
| `PRICE_UNAVAILABLE` | the recording carries no usable price |
| `PRICE_ASSET_MISMATCH` | the recording prices a different asset |
| `MARKET_IDENTITY_MISMATCH` | chain, network or provider differs |
| `PRICE_STALE` | the observation is older than the configured bound |
| `PRICE_NOT_YET_OBSERVED` | the observation is dated after the judging instant |

Closed holdings (`quantity == 0`) need no price and produce neither.

## Valuation logic

A `PositionMark` carries the price, the snapshot and observation ids it came out
of, the provider, and `observed_at` — **the instant the source itself recorded**.
Assembly time is never written there, so a mark cannot rejuvenate by being read
late. Freshness is half-open and asks the mark, not the reader:
`is_current_at(instant, tolerance)` is `0 <= age <= tolerance`, so a future
observation is as refused as an old one.

The tolerance is SENTINEL's own `RiskLimits.max_snapshot_age_seconds` — the same
object that gates every other source. Nothing here loosens it, and no limit is
relaxed to make a holding fit.

**Complete or nothing.** `PortfolioValuation.complete` is true only when every
holding was marked. A partially valued portfolio is worse than an unvalued one
because it looks like a figure; the callers refuse on it instead. Entry price,
zero and last-known-price are all absent as fallbacks.

Determinism: the same positions and the same recordings produce an identical
valuation, because nothing in the path reads the clock except the instant it is
handed, and no `uuid4` or read time enters a mark.

`portfolio_state` now quantizes exposure and the unrealised loss to the ledger's
eighteen places. A quantity and a price each carry eighteen, so their product
carries thirty-six; the sum is exact whatever order the holdings arrive in, and
only the result is rounded — so the same holdings always yield the same figure.

## Integration

Callers supply a case id and a request key. No price, portfolio value, mark or
limit crosses the boundary, in either path.

Both the risk request and the case fill:

1. value the portfolio **before** the account lock, so a provider read never
   happens while a database lock is held and no unbounded wait exists under one;
2. take the account lock, then the case lock — the established order, unchanged;
3. check `valuation.covers(held)` against the positions found *under* the lock. A
   position that came into existence in between means the valuation describes a
   portfolio that no longer exists, and `PORTFOLIO_CHANGED_DURING_VALUATION`
   refuses. No fill happens on an incomplete portfolio basis;
4. pass the marks into the single shared `portfolio_state`, which is still the
   only implementation of "what is our exposure?".

Historical replay short-circuits **before** the valuation, so a completed request
or fill returns exactly what was recorded with no market read at all — proved
against a feed that raises on contact.

## Freshness at the execution boundary

The 2M-D time boundary is unchanged and now carries one more check. All
valuation-relevant reads happen before the authoritative clock read; from there
to the verdict is synchronous.

At the actual execution boundary — the second clock read, immediately before the
pure fill simulation — `still_authorised(at)` re-checks the approval window, the
case binding, the central evaluator's verdict, the completeness reading, every
source age **and now every mark used**:
`valuation.stale_at(at, max_snapshot_age_seconds)` returns
`POSITION_VALUATION_STALE`. The persistence between the decision and the fill
costs real time, and a mark that aged out in that span is not carried over.

An expiry is a typed stop that rolls the whole transaction back. Nothing is
backdated, no deadline is extended, and an expiry never becomes a terminal risk
verdict about the market.

## Persisted evidence

The marks actually relied on are recorded with the decision they supported:
`basis["portfolio"]["position_marks"]` on the risk request, `basis["position_marks"]`
on the execution. Each entry keeps asset, pair, provider, price, snapshot id,
observation id and the source's own `observed_at`, so the valuation basis
reconstructs from the stored row alone — with no market read — and can be checked
against the sources long after they have moved.

## Test evidence

`tests/valuation/test_marks.py` — 18 contract tests over the reader itself:
pricing from its own market, several positions, a closed position needing no
price, every refusal above, the freshness boundary at 29/30/31 and −1 seconds,
determinism, complete-or-nothing, and the port's surface.

`tests/casefill/test_marks.py` — 13 integration proofs on native PostgreSQL. The
production path runs throughout: the real workflow service against a real
database, the real risk request, the real completeness check,
`src.risk.engine.evaluate`, `PaperExecutor`, the real ledger postings and the
real valuation reader.

Honestly distinguished, as in every phase since 2M-C: the **evidence is fixture
evidence**, and the market feed's read and the stop source are supplied as values
— two ports, as values. **No provider is called for real anywhere in this suite,**
and no specialist worker and no launcher runs. No sleep is used for any time
case; the controlled clock advances when work happens.

Covered, in order of the requirement:

- a filled position records the market it was acquired in;
- a holding in market A valued while a case runs in market B, with A's own
  recorded price;
- the exposure the fill-time re-check judged actually including that holding;
- three open positions in three markets, all valued;
- a missing, stale and mis-attributed recording each producing a typed stop
  naming which — `MARKET_NOT_RECORDED`, `PRICE_STALE`, `PRICE_ASSET_MISMATCH` —
  and no fill;
- the risk request refusing the same way, so no unusable basis reaches a fill;
- a position created between the valuation and the account lock: refused as
  `PORTFOLIO_CHANGED_DURING_VALUATION`, nothing written;
- a valuation ageing out during persistence before the fill: refused as
  `POSITION_VALUATION_STALE` at the boundary, with cash, fees and positions
  unchanged;
- the stored marks reconstructing a `PortfolioValuation` after a reload, with
  source instants and provenance intact;
- replay succeeding against a feed that raises on any market read;
- the account pause, idempotent replay, the `EXECUTED` transition and the ledger
  postings all still in force.

## Migration

`0009_position_market_identity` — four nullable columns on `positions` and one
index on `market_pair_id`. Additive only; nothing existing is altered and no
value is backfilled, because there is no value that could be inferred. Head is
`0009`.

## Remaining limits

- **Positions predating the migration cannot be valued.** They refuse with
  `POSITION_MARKET_UNKNOWN` rather than being guessed at.
- **One recorded market per position.** A holding acquired across several pools
  is outside this contract.
- **No exit, no re-entry, no topping up.** An executed market is still barred
  from opening another case.
- **No second valuation source.** If nothing has been recorded for a holding's
  market, the answer is a refusal, not a fallback price.
- **No launcher and no worker.** Nothing calls either service automatically.
- Live execution, signing and broadcast remain out of scope entirely.

## Hardening round

Three defects, each reproduced against `e5c5657` before being fixed.

**The case's own asset was priced by the case.** `portfolio_state` skipped every
holding whose asset was the one being traded, and `prices[asset_id]` — this
market's price — then valued it anyway. Inventory bought in pool A was marked at
pool B's price, and a holding with no recorded market at all was valued as if it
had been bought in the case's. The reproduction held four units acquired in a
second pool at 9.00 while the case ran in a pool at 1.25: the exposure SENTINEL
judged was **5**, not 36, `position_marks` was empty, and the fill went through.
`covers()` could catch neither, because it compared *which* assets had been
looked at, not which had been priced — a valuation with a refusal in it answered
`True`.

Three separate corrections:

- Every non-zero holding is valued, the order's asset included. The order's price
  now prices only the position the order would open.
- A holding is priced from the case's market only when it was *acquired* in that
  market — same pair, chain, network and provider — and a market prices one base
  asset, so a different asset is never priced by it. A holding that records no
  market is never "here": that absence is the ambiguity the identity contract
  refuses to resolve, and resolving it in favour of whichever market happens to
  be asking is the worst available answer. It refuses with
  `POSITION_MARKET_UNKNOWN` instead.
- `valued_assets` is now `considered_assets`, and the one lying predicate is
  replaced by two honest ones: `unconsidered(held)` names holdings that appeared
  after the valuation, `unusable(held)` names holdings that were looked at and
  could not be priced. They are different failures and now have different
  answers.

That leaves the case where the order's asset is already held, bought elsewhere.
Positions are one row per asset, so a fill would merge inventory from two markets
into one position recorded against one of them — one of the two records would be
false. That is a top-up across markets, and **no top-up contract exists here to
fall back on**; inventing one would be a strategy hidden in a valuation. Both
paths refuse with `POSITION_MARKET_CONFLICT`: the request too, because approving
an order that could only be filled that way would spend the case's one request on
an impossibility. The standalone paper path raises on the same condition, where
reaching it is an invariant violation rather than a decision.

**A stored rejection was not treated as history.** The short-circuit before the
valuation asked whether a *fill* existed, so a case whose fill-time re-check had
already rejected valued the portfolio all over again — and with a holding to
price and the market layer unreachable, the replay raised instead of answering.
One order gets one verdict, and a rejection is as final as a fill. The check now
asks whether the re-check's decision exists at all (`RiskRow` by the stored
request's intent), which covers both and covers a rolled-back attempt correctly
by covering neither. It remains a decision not to do work: `replay_in_session`
still answers authoritatively under the account and case locks, on the intent
identity, and a mismatched request key is still refused rather than answered.

**The stored basis could not be recomputed.** The request recorded marks and
aggregate figures; the execution recorded marks and no portfolio at all. Neither
kept the holdings the figures applied to, so checking an exposure meant re-reading
position rows that the fill itself had changed — which is not a reconstruction of
anything. `portfolio_basis` now records the holdings with their quantities, cost
bases and market attributions, the marks relied on, the account's cash and the
day's realised loss as the evaluation saw them (read before the fill books onto
the same row), the instant, the freshness bound and the market being judged.
`replay_portfolio_basis` feeds all of it back through the same `portfolio_state`,
so the recomputation *is* the computation rather than a second implementation of
it, and both bases also store the typed `RiskContext` that was handed to SENTINEL
to compare against. The execution basis takes its portfolio from the state the
re-check was actually reached from, carried out of `execute_in_session` on
`PaperOutcome`, rather than from a recomputation after the fact. Both are covered
by the existing digests: the request's whole basis by `risk_request_digest`, and
the execution's by the row it is written with.

Proved in `tests/casefill/test_marks_hardening.py` (15 tests): a holding in
another pool refusing in both paths, a holding with no recorded market refusing
in both, a refused holding no longer passing as valued, a different asset never
taking this market's price, a holding acquired in this market being priced by
this market's own reading, a stored fill-time rejection replaying with
`replayed=True` against a market layer that raises on contact and with no
bookings, completed fills and key conflicts still behaving as before, and the
stored basis of both the request and the fill recomputing the exact `RiskContext`
SENTINEL judged after the position rows have been changed underneath it.

### Follow-up: the day that had already ended

One audit discrepancy remained. The case-fill service read the account's cash and
realised loss *before* `execute_in_session`, and `roll_loss_day` zeroes the day's
loss inside that call — before `portfolio_state` and SENTINEL use it. Filled two
seconds before UTC midnight and judged four seconds after it, SENTINEL saw a
daily loss of **64** while the basis recorded the inputs for **114**: the stored
record described yesterday.

The inputs are now captured where they are used. `portfolio_state` returns the
`ValuationInputs` it was given as part of its own result, and `portfolio_basis`
takes nothing but that state — there is no longer a second set of arguments that
could differ from the first. The case fill passes `PaperOutcome.state` straight
through, so the record comes from the evaluation rather than from an account row
that has since been normalised for a new day and booked against by the fill. The
day-rolling logic itself and the execution boundary are untouched.

Proved in `tests/casefill/test_loss_day.py` on a controlled clock with no sleeps:
an approval two seconds before midnight with a real loss on the books, a fill
four seconds later inside the same approval window with every source still valid,
SENTINEL judging the new day's loss, and `replay_portfolio_basis` reconstructing
that exact `RiskContext` from the stored row — before and after the account
values and position rows are changed underneath it. The control case, filled one
second later on the same day, keeps its 50 and reconstructs just as exactly. The
request's own basis records the day it was made on.

Unchanged by this round and re-checked by the existing suites: the 2M-D execution
boundary, the freshness of every mark used at it, the account → case lock order,
full rollback on a lapsed window, the account pause, the single limits source and
the intent identity.
