# PRE_VECTOR_EARLY_ENTRY_V1 (Issue #40, part 1)

PAPER only. Off by default (`PRE_VECTOR_EARLY_ENTRY_ENABLED=false`). Part 1 of
Issue #40: entry only. The early exit contract (`EARLY_PAPER_EXIT_V1`, PR B) is
required before the strategy may be enabled at runtime.

## What does not change

- The normal path: `CURRENT_WORKFLOW = trade-case-v2`, VECTOR, its policy, its
  setup kinds and its 24-bar requirement, the normal ANCHOR ladder
  (100 / 500 / 2,500 / 10,000 / 50,000), the normal SENTINEL limits
  (minimum liquidity 100,000 USD) and the operator's configured notional.
- The scout: `--scout-once` still opens nothing, and a scout ORBIT review is
  never TradeCase evidence. An early case gets its own fresh ORBIT task.
- SENTINEL decides every early trade. Its rejection cannot be overridden.

## Identity

The strategy id `PRE_VECTOR_EARLY_ENTRY_V1` is stored in the existing
`trade_cases.strategy_policy_id` by the early intake, and the case runs the
workflow `trade-case-early-v1`. Neither is ever inferred from market data.

## Candidates

`EarlyWatchCandidates` reads WATCHING (never PROMOTABLE) Robinhood watches first
seen within six hours, youngest first, skipping fixtures, markets with a live
case, markets COMMANDER bars (RISK_REJECTED / EXECUTED), and any market that
already had an early case (one early attempt per market, in any outcome). No
market cap, FDV, volume, trending, liquidity or quote-asset filter. The intake
is COMMANDER's, restricted to Robinhood, one case per cycle, with a one-hour
`case_lifetime`. As everywhere in COMMANDER, `expires_at` is
`candidate.observed_at + case_lifetime` — measured from the observation the
case was opened on, so slightly less than an hour after the insert.

## Workflow `trade-case-early-v1`

Required: ORBIT (discovery), ATLAS (on-chain, unchanged rules; funding V2 stays
shadow), EARLY (TRADE_SETUP, safety-critical), PULSE (trigger), ANCHOR
(liquidity/execution). SIGNAL is not used in V1. No VECTOR task.

## EARLY

Deterministic, no model, no provider of its own. In order:

1. case must be an early case of the early workflow (`EARLY_STRATEGY_MISMATCH`);
2. usable ATLAS evidence (else wait `ATLAS_EVIDENCE_PENDING`) carrying the
   chain-side creation block, timestamp and source
   (`EARLY_CREATION_TIME_UNAVAILABLE`);
3. `0 <= now - creation_timestamp <= 6h` (`EARLY_CREATION_TIME_IN_FUTURE`,
   `EARLY_CANDIDATE_TOO_OLD`); never the scout's first-seen time;
4. a fresh recorded price for the case's own market (else wait
   `MARKET_OBSERVATION_PENDING`);
5. VECTOR's own `assess`: `SUFFICIENT` → `EARLY_VECTOR_HISTORY_SUFFICIENT`
   (the normal path owns it); only `MARKET_HISTORY_TOO_SHORT` and
   `MARKET_HISTORY_EMPTY` are admitted; identity, price-basis and timeframe
   mismatches are refused with VECTOR's code;
6. a non-empty young series is assessed again with the bar minimum lowered to
   one and every other threshold unchanged, so in-future, stale and gapped
   series are refused.

Setup: BUY, kind `PRE_VECTOR_EARLY_ENTRY` (not a VECTOR `SetupKind`), zone and
`PRICE_IN_RANGE` trigger `[0.95 P, 1.05 P]`, `valid_from` = decision time,
expiry ten minutes later, invalidation `0.40 P`, one informative target `2 P`
(not a take-profit). Levels are rounded inward at 18 places; a price too small
to express is refused (`EARLY_PRICE_PRECISION_UNSUPPORTED`). The setup carries
`early_entry` with strategy, workflow, both sufficiency verdicts, closed bars,
timeframe/aggregate, creation block/time/source, ATLAS evidence id, age, max
age, snapshot and price observation ids, reference price, decision time; plus
the setup fingerprint and policy version.

An ineligible market is a permanent task failure with its reason; the case
expires and is the no-entry baseline.

## ANCHOR `early-anchor-execution-v1`

Every integrity bound of `anchor-execution-v1` (quote age, reference age,
skew, deviation, provider impact, hops, valuation skew). Ladder
10 / 25 / 50 / 100 / 250 USD, each rung rounded up in payment-asset units so
the first rung tests at least ten dollars. Any quote asset is quoted in its own
units.

## Risk request and sizing

- Limits: `limits_for(strategy)` — for an early case only
  `min_liquidity_usd = 10,000`; every other SENTINEL limit is the account's.
- Notional: fixed $10, never the operator's configured size, never downsized.
  If ANCHOR did not prove at least $10, the request is refused before SENTINEL
  (`EARLY_EXECUTABLE_CAPACITY_INSUFFICIENT`, detail `…_UNKNOWN` /
  `…_BELOW_NOTIONAL`).
- Caps: 5 open early positions, $50 early exposure (existing + $10), $30
  early realised loss since the start of the UTC day (early exits only). They
  are judged twice, both times under the paper account lock and before
  SENTINEL is asked: at the risk request (`EARLY_STRATEGY_CAP_REACHED` on the
  request) and again at the fill, on the ledger as it stands in the fill's own
  transaction (`ExecutionRefusal.EARLY_STRATEGY_CAP_REACHED`, detail names the
  cap). An approval reserves nothing, so two early cases approved against the
  same free slot are settled at the fill: every fill and every exit takes the
  account lock first, so the second fill reads the first one's position. A cap
  never engages the kill switch and is not a risk verdict.
- Exposure is the ledger's cost basis, which includes paper slippage and
  fees: at 25 bps slippage and 30 bps fees one $10 entry books
  10.055075 USD. Four entries hold 40.22 USD, so a fifth (40.22 + 10 > 50) is
  refused by the exposure cap; with any positive cost the $50 exposure cap
  binds before the five-position cap. $50 is a hard cost-basis cap.
- Two exposure checks. The request and the start of the fill add the nominal
  $10 (`cap_refusal`) — a conservative pre-filter. The authoritative one
  (`booked_exposure_refusal`) runs inside the fill: the PAPER executor
  simulates the fill, the ledger's own `apply_fill` computes the position it
  would book, and before anything is written the added cost basis (gross +
  fees + gas) must keep early exposure at or below $50. Otherwise the fill is
  rolled back with `EARLY_STRATEGY_CAP_REACHED` / `EARLY_MAX_EXPOSURE_REACHED`.
  Example: 40.00 held, nominal 40 + 10 = 50 passes the pre-filter, the fill
  would book 10.055075 → 50.055075 > 50 → refused; at zero costs it books
  exactly 50.00 and is allowed. Nothing is ever downsized to fit.
- One new early entry per bounded run (`EARLY_RUN_ENTRY_LIMIT_REACHED`).
- No re-entry: a closed early cycle is refused
  (`STRATEGY_REENTRY_NOT_PERMITTED`).
- The fill is the existing PAPER executor, re-checked against the same
  early limits.

No new table and no migration: positions, executions and exits already join to
the case and its `strategy_policy_id`.

## Configuration

`PRE_VECTOR_EARLY_ENTRY_ENABLED=true` requires `PAPER_RUNNER_ENABLED`,
`EARLY_SCOUT_ENABLED` and `ATLAS_FUNDING_GRAPH_ENABLED`. Preflight then also
requires every role of the early workflow.
