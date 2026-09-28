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
position. The holder-concentration bound needs fresh ATLAS evidence, which a
filled case does not receive, so it is not re-evaluated here.

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

## Known limits of the existing exit path (not relaxed)

- **Entry-evidence freshness.** The SELL check judges the case's ATLAS evidence
  against `max_snapshot_age_seconds` (30 s by default), and a filled case is
  terminal and cannot take new evidence. After that window every triggered exit
  is refused with `SOURCE_OLDER_THAN_RISK_LIMIT` (later `RISK_DATA_INCOMPLETE`)
  and the position stays open. Making auto exits executable hours after entry
  needs a separate decision on how a held position's risk data is refreshed for
  the exit; loosening the bound is not an option.
- **Invalidation vs. SENTINEL.** SENTINEL's liquidity floor binds sales too, so
  a `SENTINEL_INVALIDATION` exit is rejected by the SELL check
  (`EXIT_RISK_REFUSED`). The trigger is reported; nothing is sold.

Refusals are counted per code in the run summary (`exits.refusals`).

## Undecided production values

No defaults exist; enabling without all three fails configuration:

- `PAPER_EXIT_STOP_LOSS_BPS`
- `PAPER_EXIT_TAKE_PROFIT_BPS`
- `PAPER_EXIT_MAX_HOLDING_MINUTES`

Also to confirm: `PAPER_EXIT_INVALIDATE_BELOW_MIN_LIQUIDITY` (default `true`)
and `PAPER_EXIT_MAX_PER_RUN` (default 5, an operational bound). Tests use
fixture values only.
