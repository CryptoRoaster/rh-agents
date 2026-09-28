# PAPER auto exits: policy `PAPER_EXIT_V1`

**PAPER only, deterministic, off by default.** No model is asked, nothing signs
or broadcasts, and there is no second exit path: every sale goes through the
existing `PaperExitService.execute_position_exit` and its fresh SENTINEL SELL
check. Re-entry is not part of this.

## What runs

A full PAPER run (`--once`) with `PAPER_AUTO_EXIT_ENABLED=true` sweeps open
positions **before** promotion and intake (`AutoExitService`, bounded by the
run's step timeout and `PAPER_EXIT_MAX_PER_RUN`). For each open, case-bound
position it reads:

| Input | Source | Unknown when |
|---|---|---|
| entry price, entry time | the case fill (`trade_case_executions`) | no case-bound fill → `POSITION_ORIGIN_UNKNOWN`, not evaluated |
| mark | `PositionValuationReader`, the same marks SENTINEL values with | older than `max_snapshot_age_seconds` |
| liquidity | latest market snapshot of the held pair | not `AVAILABLE` or stale |

and evaluates `PAPER_EXIT_V1` (`src/orchestration/exitpolicy/policy.py`), first
match wins:

1. `STOP_LOSS` — mark ≤ entry × (1 − stop).
2. `SENTINEL_INVALIDATION` — known liquidity below SENTINEL's own
   `min_liquidity_usd` (switchable, `PAPER_EXIT_INVALIDATE_BELOW_MIN_LIQUIDITY`).
3. `TAKE_PROFIT` — mark ≥ entry × (1 + target).
4. `TIME_EXIT` — held ≥ maximum holding time (needs no price).
5. otherwise hold (`HOLD`, or `HOLD_MARK_UNKNOWN`).

Unknown data never triggers the exit it cannot prove: no fresh mark, no price
exit; no fresh liquidity, no invalidation. Protective triggers come first.

**SENTINEL mapping.** Only the liquidity floor maps cleanly onto a held
position as a trigger; the holder-concentration bound is an entry verdict and
not a trigger (see "BUY vs. SELL" below).

## What is stored

On the existing `trade_case_exits` row (migration 0018): `exit_trigger`,
`exit_policy_version` (both or neither, a database check) and
`exit_trigger_basis` (policy values, inputs, verdict — recomputable by the same
function). Entry price and time, exit price and time, quantity, fees, released
cost basis and `realized_pnl_usd` are the ledger's, as for every exit. Manual
exits leave the trigger columns empty.

## Idempotency

Key `auto-exit:PAPER_EXIT_V1:<cycle>:<UTC minute>`: a retry inside the minute
finds its own order. The database allows one exit per cycle, so no later run can
sell a holding twice; a closed position is no longer swept.

## The exit's own risk read

A sale is never judged on the entry's evidence: that belongs to a terminal case,
ages past SENTINEL's 30 s bound within seconds and must not be reopened. Each
exit takes its own read, before the account lock:

- **On-chain:** `AtlasExitRead` — ATLAS's deterministic half only: the same
  chain, holder and origin ports, `AtlasSnapshotBuilder`, the versioned ATLAS
  policy and the same payload derivation the worker submits. No model, nothing
  written to the case. Stored on the exit as `basis.exit_onchain` (policy
  version, snapshot digest, payload) with `basis.exit_basis = FRESH_EXIT_READ`.
- **Market:** the held pair's latest recorded snapshot (the PAPER run's
  acquisition re-observes markets of open positions before the sweep).
- **Routing:** the pool the holding was bought in, observed again with
  liquidity > 0; otherwise `UNKNOWN`.

Missing inputs refuse the sale: `EXIT_READ_UNAVAILABLE` (no chain source, read
failed, or read for another case) and `EXIT_DATA_INCOMPLETE` (no market,
price, token metadata, cost basis or holder measurement). Stale inputs refuse
with `SOURCE_OLDER_THAN_RISK_LIMIT`. With auto exits enabled the service is
always composed with a fresh read; without a chain source it is
`UnavailableExitRead`, never a fallback to entry evidence. A service composed
without any exit read (manual exits, as before) still judges on the entry's
evidence and refuses once that is older than the bound.

## BUY vs. SELL in SENTINEL

`src/risk/engine.py` judges a BUY exactly as before. For a SELL the two
entry-quality bars no longer apply:

| Check | BUY | SELL |
|---|---|---|
| minimum pool liquidity (`min_liquidity_usd`) | reject below | reject only if unknown or ≤ 0 |
| holder concentration limit / holder `FAIL` | reject | not applied |
| holder domain `UNKNOWN`, holder metrics unknown | reject | reject |
| token contract integrity, routing, accounting | reject unless PASS | reject unless PASS |
| stale/future market or safety data, intent timing | reject | reject |
| slippage, fees, portfolio data, kill switch, mode, daily loss | reject | reject |
| insufficient position | – | reject |

A concentrated or thinning market is a reason to leave, not to stay.
Everything that decides whether a sale is possible and correctly valued still
binds, and every UNKNOWN still fails closed.

Refusals are counted per code in the run summary (`exits.refusals`).

## Undecided production values

No defaults exist; enabling without all three fails configuration:

- `PAPER_EXIT_STOP_LOSS_BPS`
- `PAPER_EXIT_TAKE_PROFIT_BPS`
- `PAPER_EXIT_MAX_HOLDING_MINUTES`

Also to confirm: `PAPER_EXIT_INVALIDATE_BELOW_MIN_LIQUIDITY` (default `true`)
and `PAPER_EXIT_MAX_PER_RUN` (default 5, an operational bound). Tests use
fixture values only.
