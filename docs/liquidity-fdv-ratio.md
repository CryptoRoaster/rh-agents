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
| `0.10 <= ratio <= 1` | none | judged on the other bars |
| `ratio < 0.10` | `LIQUIDITY_FDV_RATIO_LIMIT` | REJECT |
| `ratio > 1` | `LIQUIDITY_FDV_RATIO_UNVERIFIED` | REJECT |
| supply missing or zero, or contract decimals missing | `FDV_UNKNOWN` | REJECT |
| contract decimals differ from the provider's | `FDV_DECIMALS_MISMATCH` | REJECT |
| pool liquidity unknown | `LIQUIDITY_UNKNOWN` (the ratio is not judged again) | REJECT |

Both bounds are compared exactly, at 160 significant digits, so no rounding
can carry a ratio across either. A ratio of exactly 10 % passes. The 100 %
ceiling is judged at the lowest price the BUY's rounded-up ledger price can
stand for (`price_usd` minus one ledger unit, 1e-18), so a ratio of exactly
100 % at the ledger price is refused, and so is any BUY priced at one ledger
unit.

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
- **Price:** the risk snapshot's `price_usd`, which for a BUY is the recorded
  price rounded up to the ledger unit (1e-18). The floor is judged at that
  price, where the FDV can only come out larger. The ceiling is judged at
  `price_usd - 1e-18`, where it can only come out smaller. Both err toward
  refusal. This matters only for prices near 1e-15 USD and below, where one
  ledger unit is a large share of the price.
- **Liquidity:** `LiquiditySnapshot.liquidity_usd`, the provider's
  `reserve_in_usd` floored to the ledger unit. It counts both sides of the pool.
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

The case still ends `RISK_REJECTED`, as after every SENTINEL rejection, and
COMMANDER intake then bars the market (`RISK_REJECTED_FOR_MARKET`). That bar is
the existing rule for all rejections and is not specific to this gate.

## SELL

A sale is never judged on the ratio, the FDV or the decimals. Its market view
carries neither supply field, so its snapshot keeps the shape and fingerprint
it always had. An exit is never trapped by this gate.

## Recorded shape

`TokenSnapshot.total_supply_raw` / `total_supply_decimals`,
`HolderDistributionFacts.total_supply_decimals` and
`RiskMetrics.liquidity_fdv_ratio` are omitted from serialization when absent.
Snapshots, evidence and decisions stored before this gate keep their bytes and
fingerprints. Holder evidence written before the contract decimals were carried
yields `FDV_UNKNOWN` for a BUY. A new BUY needs fresh evidence anyway.
