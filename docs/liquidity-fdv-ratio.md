# Liquidity/FDV entry gate

Status: enforced by SENTINEL on every BUY. **Never on a SELL.** No migration, no
new setting, no provider or RPC request of its own.

## Rule

A purchase is judged on how deep its pool is against what the whole token is
valued at. A pool that is large in dollars but thin for its valuation cannot
absorb an exit.

```
ratio = pool liquidity (USD) / FDV (USD)
FDV   = total_supply_raw / 10^total_supply_decimals * price_usd
```

| Case | Reason code | BUY |
|---|---|---|
| `0.10 <= ratio <= 1`, proven | none | judged on the other bars |
| `ratio < 0.10` possible | `LIQUIDITY_FDV_RATIO_LIMIT` | REJECT |
| `ratio > 1` at the snapshot's figures | `LIQUIDITY_FDV_RATIO_UNVERIFIED` | REJECT |
| `ratio <= 1` at the figures, but rounding leaves `> 1` possible | `LIQUIDITY_FDV_RATIO_UNDECIDABLE` | REJECT |
| supply missing or zero, or contract decimals missing | `FDV_UNKNOWN` | REJECT |
| contract decimals differ from the provider's | `FDV_DECIMALS_MISMATCH` | REJECT |
| pool liquidity unknown | `LIQUIDITY_UNKNOWN` (the ratio is not judged again) | REJECT |

Both bounds are compared exactly, at 160 significant digits, with no float
and no tolerance.

- **10 %:** exactly 10 % passes. The floor is judged on the lowest ratio the
  figures allow, so rounding can only refuse, never admit.
- **100 %:** exactly 100 % passes when both figures are exact, that is
  `MarketSnapshot.price_exact` and `LiquiditySnapshot.liquidity_exact` are
  both true. The ledger stores 18 places: a BUY's price is the recorded price
  rounded up, its liquidity the recorded reading floored. Where either was
  rounded, the true ratio may lie above the figures, with price and
  liquidity each off by less than one ledger unit. The ceiling is then
  judged on the highest ratio they allow (liquidity plus one ledger unit over
  supply times price minus one ledger unit). If that exceeds 100 % while the figures themselves do not, the
  decision is undecidable and refused as `LIQUIDITY_FDV_RATIO_UNDECIDABLE`,
  kept apart from a measured `UNVERIFIED`. This matters only at prices near
  1e-15 USD and below, or within one ledger unit of the bound. A price rounded
  up to one ledger unit can never pass; a recorded price of exactly one
  ledger unit is exact and judged like any other.
- One code is reported per BUY, in this order: `UNVERIFIED`, then
  `UNDECIDABLE`, then `LIMIT`. A BUY refused as `UNVERIFIED` or `UNDECIDABLE`
  may also lie below the floor; it is refused either way.
- Absent flags (any snapshot not built by `market_view` for a BUY, and every
  snapshot recorded before them) read as rounded.

The floor is `RiskLimits.min_liquidity_fdv_ratio` (default `0.10`). It must lie
in `(0, 1]`, so it can be tightened but not switched off. The 100 % ceiling is
fixed. The early strategy (`limits_for`) changes only `min_liquidity_usd`; the
ratio applies to early entries unchanged.

## Inputs and provenance

- **Supply:** `HolderDistributionFacts.total_supply_raw`, ATLAS's own
  `totalSupply()` read over RPC at the pinned contract block. It is the same
  denominator the holder concentration was divided by. No provider market cap
  or FDV figure is used.
- **Decimals:** `HolderDistributionFacts.total_supply_decimals`, the contract's
  own `decimals()` from that same read. The provider's decimals
  (`BaseAssetMetadata`, used for sizing) must match it. On a mismatch one of the
  two scales is wrong and the FDV is off by powers of ten, so the BUY is
  refused rather than guessed.
- **Price:** the risk snapshot's `price_usd`, for a BUY the recorded price
  rounded up to the ledger unit (1e-18), with `price_exact` saying whether
  rounding changed it.
- **Liquidity:** `LiquiditySnapshot.liquidity_usd`, the provider's
  `reserve_in_usd` floored to the ledger unit, with `liquidity_exact` saying
  whether flooring changed it. It counts both sides of the pool.
- **Freshness:** supply and decimals sit on SENTINEL's `TokenSnapshot`, whose
  timestamp is the provider's metadata observation, not the contract read.
  They are bound in time only indirectly: they come from the same ATLAS
  evidence as the `HolderSnapshot`, which is dated by the holder source's
  observation and held to `STALE_OR_FUTURE_SAFETY_DATA`. The contract read's
  block can differ from that observation by `holder_block_delta`. A mint
  between the ATLAS read and the BUY would understate the FDV, within that
  evidence-age window.

## A ratio above 100 %

Liquidity counts both pool sides, so it can exceed the FDV legitimately: a
fresh launch whose pool still holds nearly the whole supply. It can also come
from wrong data, such as a stale price or a liquidity figure from another pool.
SENTINEL cannot tell the two apart from the snapshot alone.

The reading is therefore treated as **unverified risk evidence, not as a
finding against the token**:

- The reason code says `UNVERIFIED`. No safety domain turns `FAIL`, and ATLAS
  evidence is not changed.
- The ratio is still recorded in `RiskMetrics.liquidity_fdv_ratio`, so the
  decision can be audited.
- A SELL against the same market is unaffected.

## After a rejection

SENTINEL keeps no memory: every verdict is computed from the figures in front
of it, and no earlier approval or refusal is reused.

The case still ends `RISK_REJECTED`, as after every SENTINEL rejection, and
COMMANDER intake then bars the market (`RISK_REJECTED_FOR_MARKET`); the scout
skips it too. That bar is unchanged. A market refused only by this gate can be
asked about again through one explicit call,
`LiquidityFdvRecheckService.open_recheck(predecessor_case_id)`
(`src/orchestration/recheck`, issue #124). Nothing calls it on a schedule, and
it opens a case, never a purchase.

`LIQ_FDV_RECHECK_V1`:

| Rule | Value |
|---|---|
| Predecessor | `RISK_REJECTED`; its latest binding (highest `case_revision`) carries the case's own `risk_input_digest`, outcome `REJECT` (not `PAUSE_SYSTEM`) |
| Eligible codes | a non-empty subset of `LIQUIDITY_FDV_RATIO_LIMIT`, `LIQUIDITY_FDV_RATIO_UNVERIFIED`, `LIQUIDITY_FDV_RATIO_UNDECIDABLE`, `FDV_UNKNOWN`. Any other code, `FDV_DECIMALS_MISMATCH` included, keeps the market barred |
| Market history | the predecessor is its newest case, no case is live, none was executed, and every other rejection in it was gate-only too |
| Window | from 30 minutes to 24 hours after the rejecting decision's `evaluated_at` |
| Per rejected case | one successor. A second call returns the same case (`replayed`), never a new one |
| Per market | at most 2 successors over its whole history, however each ended. A successor refused on gate codes alone is itself eligible, with its own window, so a market gets at most three SENTINEL verdicts |
| Operational bars | the chain is one intake opens cases on, the market is not a fixture, and under `PAPER_EARLY_ONLY` only an early predecessor is re-checked |
| Stops | kill switch, PAPER mode, configured pause, durable pause, entry halt: as for any entry |
| Successor | a new case through `open_trade_case_in_session`, in the workflow's starting state, under the predecessor's workflow and strategy, living one hour. Its `originating_discovery_reference` is the predecessor's case id |

The successor inherits nothing: no evidence, binding, risk request or
approval. ORBIT, ATLAS, PULSE, ANCHOR and SENTINEL run again on evidence
gathered after the call, and every BUY gate applies in full; a ratio still out
of band is rejected again. A successor that lapses or is cancelled ends the
history; nothing re-checks it.

For an early market this is a second early case for the same pool, the one
exception to "one early attempt per market". It is not a re-entry: no cycle
completed, and the early age bound and caps still apply.

**No migration.** The successor's open key is
`rh-agents:liq-fdv-recheck:<predecessor id>` and its case id is derived from
that key, so the unique open key already enforces one successor per rejected
case; the per-market cap counts those keys; the account lock serialises
callers. Its first event, `LIQ_FDV_RECHECK_OPENED`, records the policy
version, the predecessor, the binding and the codes it was eligible on. The
predecessor's case, bindings, risk request and events are never written.

## Calibration

Whether 10 % is the right floor can only be read from figures that were
observed together. `uv run python -m src.evaluation.liquidity_fdv --database
NAME --port PORT` reads a local database read-only and prints JSON:

- every ATLAS contract read (`total_supply_raw`, `total_supply_decimals`, its
  block) paired with the nearest market observation of the same pair within
  30 seconds (at most ten minutes), judged by `liquidity_fdv_verdict`, the
  same function SENTINEL calls, after the same BUY rounding;
- everything else under the reason it is UNKNOWN (`SUPPLY_EVIDENCE_MISSING`,
  `EVIDENCE_NOT_COMPARABLE`, `MARKET_OBSERVATION_MISSING`, `PRICE_UNKNOWN`,
  `LIQUIDITY_UNKNOWN`, `FDV_UNKNOWN`, ...), never with a ratio;
- a ratio spread only from 30 measured ratios on;
- pool age at first sight from `discovery_watches.pool_created_at`, UNKNOWN
  where it is absent.

No provider, RPC or model is called, and a provider's market cap or FDV is
never read. As of 2026-10-27 no local database holds an ATLAS contract read
for a Robinhood market, so every market reads `SUPPLY_EVIDENCE_MISSING`: the
supply and contract decimals come only from ATLAS inside a PAPER case.

## SELL

A sale is never judged on the ratio, the FDV or the decimals. Its market view
carries neither supply field, so its snapshot keeps the shape and fingerprint
it always had. An exit is never trapped by this gate.

## Recorded shape

`TokenSnapshot.total_supply_raw` / `total_supply_decimals`,
`MarketSnapshot.price_exact`, `LiquiditySnapshot.liquidity_exact`,
`HolderDistributionFacts.total_supply_decimals` and
`RiskMetrics.liquidity_fdv_ratio` are omitted from serialization when absent.
Snapshots, evidence and decisions stored before this gate keep their bytes and
fingerprints. Holder evidence written before the contract decimals were carried
yields `FDV_UNKNOWN` for a BUY. A new BUY needs fresh evidence anyway.
