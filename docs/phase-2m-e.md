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
