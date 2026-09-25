# Market decimal precision

Three domains, one rule each.

| Domain | Examples | Precision |
|---|---|---|
| Recorded market facts | snapshot price/liquidity/volume, OHLCV bars, a monitor's observed price, ANCHOR's reference and payment-asset prices, and the same values where evidence payloads repeat them | The market layer's own contract (`src.markets.models.Amount` / `MarketPrice`: up to 100 coefficient digits, exponent within ±1000). Copied exactly, never quantized on read. |
| Model and strategy outputs | VECTOR entry, invalidation, targets and trigger levels; configured notionals, bps and policy floors | Their own bounded contract (18 decimal places). A model does not gain precision because a market reported more. |
| Risk, execution, accounting | SENTINEL's `src.core.models.MarketSnapshot`, fills, positions, exposure and loss figures, the ledger | `Numeric(38, 18)`. A recorded fact enters only through an explicit conversion at the boundary. |

The risk boundary is `risk_market` in `src/orchestration/riskrequest/service.py`.
Every conversion there goes in the direction that can only make the order look
worse, never by as much as one ledger unit:

- the reference price is rounded **up** (`quantize_up`) for a buy and **down**
  (`quantize_down`) for an exit. A buy is sized from the exact recorded price
  with a floored quantity, and SENTINEL checks `quantity * price_usd`; a lower
  ledger price would understate that notional;
- a price below the smallest ledger unit (`1e-18`), or too large for
  `Numeric(38, 18)` before or after rounding, is refused with
  `REFERENCE_PRICE_OUTSIDE_ACCOUNTING_PRECISION`. It is never zeroed, raised to
  the smallest unit or capped;
- liquidity goes through `quantize_down`, so it can only look thinner and can
  never be rounded over `min_liquidity_usd`; a value too large for the ledger is
  refused with `LIQUIDITY_OUTSIDE_ACCOUNTING_PRECISION`, never capped.

Position marks (`PositionMark.price_usd`) are recorded market prices and keep
market precision. The accounting boundary for valuation is the result:
`portfolio_state` multiplies quantity by the exact mark and sums in a wide
context (`EXACT_VALUATION_PRECISION = 250`), and only exposure, unrealized loss
and the day's loss are brought to 18 places:

- rounded **up** (`quantize_up`), because SENTINEL rejects when exposure plus the
  order exceeds its limit and when the day's loss reaches its limit, so a figure
  rounded down could pass a check the exact value fails;
- a figure the ledger cannot hold, before or after rounding, becomes a typed
  `PortfolioAccountingIssue` (`EXPOSURE_OUTSIDE_ACCOUNTING_PRECISION`,
  `DAILY_LOSS_OUTSIDE_ACCOUNTING_PRECISION`) with accounting `UNKNOWN`. It is
  never truncated, capped or treated as a missing mark, and the marks and prices
  that produced it stay in the state for audit;
- RiskRequest, CaseFill and PaperExit refuse such a portfolio with
  `PORTFOLIO_ACCOUNTING_UNREPRESENTABLE` before SENTINEL is asked and before any
  fill; the standalone PAPER service, which has no refusal layer, passes the
  unknown accounting to SENTINEL, which rejects it.

Derived execution values in ANCHOR (notionals, token amounts, effective prices,
deviations) were already quantized where they are computed and are unchanged.
