# Market decimal precision

Three domains, one rule each.

| Domain | Examples | Precision |
|---|---|---|
| Recorded market facts | snapshot price/liquidity/volume, OHLCV bars, a monitor's observed price, ANCHOR's reference and payment-asset prices, and the same values where evidence payloads repeat them | The market layer's own contract (`src.markets.models.Amount` / `MarketPrice`: up to 100 coefficient digits, exponent within ±1000). Copied exactly, never quantized on read. |
| Model and strategy outputs | VECTOR entry, invalidation, targets and trigger levels; configured notionals, bps and policy floors | Their own bounded contract (18 decimal places). A model does not gain precision because a market reported more. |
| Risk, execution, accounting | SENTINEL's `src.core.models.MarketSnapshot`, fills, positions, the ledger | `Numeric(38, 18)`. A recorded fact enters only through an explicit conversion at the boundary. |

The risk boundary is `risk_market` in `src/orchestration/riskrequest/service.py`:

- the reference price goes through `src.core.numbers.quantize`; a positive price
  that becomes zero there is refused with
  `REFERENCE_PRICE_OUTSIDE_ACCOUNTING_PRECISION`, never passed on as zero or
  raised to the smallest step;
- liquidity goes through `quantize_down`, so it can only look thinner and can
  never be rounded over `min_liquidity_usd`.

Derived execution values in ANCHOR (notionals, token amounts, effective prices,
deviations) were already quantized where they are computed and are unchanged.
