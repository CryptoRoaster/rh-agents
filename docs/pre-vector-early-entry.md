# PRE_VECTOR_EARLY_ENTRY_V1 and EARLY_PAPER_EXIT_V1 (Issue #40)

PAPER only. Both off by default (`PRE_VECTOR_EARLY_ENTRY_ENABLED=false`,
`EARLY_PAPER_EXIT_ENABLED=false`). Early entries cannot be enabled without the
early exit contract; the exit may run on its own.

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

## Exit: EARLY_PAPER_EXIT_V1

A deterministic sweep over open positions whose cycle an early case opened,
run in the bounded PAPER run right after the normal exit sweep. The normal
`PAPER_EXIT_V1` sweep never closes an early position
(`EARLY_POSITION_OWN_EXIT_POLICY`), and this sweep never closes a normal one.

Every PAPER exit reads the **held market's own stream** (`src/markets/scope.py`,
Issue #75): `MarketReader.latest_in(MarketScope)` returns the newest current
reading of the scoped provider's stream of the pool, with the same ranking and
freshness as `latest`, and accepts it only if it is of that market. The mark
(`PositionValuationReader`), the liquidity reads of both exit sweeps, the
completeness check and the sale in `PaperExitService` all use it; another
provider's newer reading of the same pool is never selected, and without an
own current reading the answer is UNKNOWN/UNAVAILABLE, never a fallback.
`latest(pair_id)` is unchanged for every other consumer.

`PaperExitService` — the one exit boundary for normal and early exits — still
checks the selected reading once more before anything is priced or sold: its
`MarketIdentity` must equal the case's (`describes_market`: provider, chain,
network, pool, assets, venue, fixture flag; a case without a pool locator
accepts one that adds it). Any other reading is refused as
`EXIT_MARKET_IDENTITY_MISMATCH` before anything is written.

Every exit is a **full** exit through the existing `PaperExitService`: whole
holding, fresh ATLAS exit read, SENTINEL SELL check, one order per key, one
exit per cycle enforced by the database, fill + ledger + exit record in one
transaction under the paper account lock. The trigger, the policy version and
every number it was decided on are stored on that exit
(`exit_trigger`, `exit_policy_version`, `exit_trigger_basis`). No migration.

| Order | Trigger | Condition |
|---|---|---|
| 1 | `STOP_LOSS` | mark ≤ 0.40 × entry cost per unit (−60 %) |
| 2 | `LIQUIDITY_INVALIDATION` | fresh liquidity known and < 10,000 USD |
| 3 | `TRAILING_STOP` | peak ≥ 2 × entry cost per unit, and mark ≤ 0.5 × peak |
| 4 | `TIME_EXIT` | held ≥ 72 h |

All conditions that hold are recorded (`verdict.conditions`); the first names
the exit.

- **Entry instant:** the case fill's `filled_at`.
- **Entry cost per unit:** the ledger's cost basis ÷ quantity — execution
  price with slippage, fees and gas.
- **Mark:** the held market's latest recorded observation, only if within
  SENTINEL's snapshot age (the same valuation reader every exit uses).
- **Liquidity:** only from the held market's own fresh reading — same pool and
  the chain, network and provider the position recorded, the rule the mark is
  held to. Another provider's reading of the same pool never invalidates.
- **Peak:** never stored. The highest available, non-fixture price recorded
  for the held market (same provider and pair) between the entry instant and
  now, together with the mark. Observations are durable, append-only rows, so
  the peak and the trailing state survive restarts by construction; the exit
  basis names the observation id and time it rested on.
- **Peak completeness:** the whole window since entry is searched, never a
  recent slice. The database orders every matching observation (pool,
  provider, window, `available`, `price.status = AVAILABLE`, price > 0,
  fixtures excluded) by its recorded USD price and only the top candidates are
  loaded (pages of 20, at most 5); each candidate is re-read as the recorder's
  `MarketSnapshot` and its price checked exactly, so the ordering only
  proposes. No migration: the price is read through the portable JSON path
  (`#>>` on PostgreSQL, `json_extract` on SQLite). On PostgreSQL only text
  shaped like a bounded number (digits, optional fraction, optional short
  exponent — `Decimal`'s own forms, e.g. `4.5E-7`) is ever cast, so damaged
  text cannot abort the query; SQLite's cast never raises.
- **Damaged data is never a complete history.** Every row in the window that
  claims an available price is counted, and so is every row whose price is a
  positive number the ranking can use; any difference — non-numeric, empty,
  missing, null, negative, NaN — marks the peak as a lower bound
  (`peak.truncated = true`). So does a candidate that cannot be read as a
  `MarketSnapshot` or whose price is at or above the ledger bound of 10^20 USD;
  such a row is skipped, never the peak. A trailing exit on a lower bound is
  still correct (the true peak, and so its trailing level, can only be
  higher); a hold on one is reported as `EARLY_EXIT_PEAK_INCOMPLETE`.
- **Missing or stale data:** an unknown mark fires no price trigger and is
  reported (`EARLY_EXIT_MARK_UNKNOWN`); unknown liquidity fires no
  invalidation. The time exit needs neither — but the sale still needs a
  current price and a fresh exit read, so a time exit that cannot be executed
  safely is refused by name, not booked, and asked again every sweep.
- **Retries and parallel sweeps:** the request key is
  `auto-exit:EARLY_PAPER_EXIT_V1:<cycle>:<UTC minute>`; a replay of the same
  key returns the recorded sale (reported as `EXIT_ALREADY_RECORDED`, never
  counted twice), and any other key for the same cycle is refused by the
  one-exit-per-cycle constraint.
- **Early ledger:** each exit's realised result is the exit row's
  `realized_pnl_usd`; losses since the UTC day start count toward the $30
  early daily loss cap.
- **No re-entry:** a closed early cycle is refused
  (`STRATEGY_REENTRY_NOT_PERMITTED`).
