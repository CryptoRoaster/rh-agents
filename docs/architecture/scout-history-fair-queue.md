# Scout VECTOR history: fair queue on its own transport

Measured on 2026-09-29: 1,200 watches due for their history check, 9 checks
ever. History shared the scout's single GeckoTerminal transport (7 requests a
run, spent on directory, discovery and refresh first), needed a fresh market
reading it never used, had one slot a run, and a failed read kept its due time —
so the oldest due watch, rate-limited, held that slot run after run.

## What changed

- **No fresh reading, no refresh.** A history check needs the stored watch
  identity (with its pool locator), a configured chain and a due checkpoint.
  It no longer passes through ORBIT's selection or refresh and spends none of
  `EARLY_SCOUT_MAX_REFRESH_MARKETS_PER_RUN`. The verdict was always formed on
  the OHLCV series alone; VECTOR `assess` and its policy are unchanged.
- **Its own transport.** Like the outcome sampler, history reads go through a
  GeckoTerminal transport of their own: `EARLY_SCOUT_HISTORY_MAX_REQUESTS_PER_RUN`
  (default 6) requests, `EARLY_SCOUT_HISTORY_REQUEST_SPACING_SECONDS` (default 6)
  apart, serial. The network id comes from the discovery directory already
  resolved in the run. Discovery/refresh and outcome budgets are unchanged; no
  global GeckoTerminal limit was raised.
- **Two lanes** (`src/scout/history_queue.py`): CURRENT (due within the last
  hour, newest first) and CATCH-UP (older, oldest first), five to one for six
  slots; an empty lane lends its slots. Chains are interleaved. The order uses
  due time, chain and pair identity only — no JEV, ORBIT, price, market cap,
  liquidity, volume or momentum.
- **Bounded retry** (migration 0020): a failed read sets
  `history_retry_not_before` after 15, 30, 60, then at most 120 minutes, with
  `history_failure_count` (≤ 10) and `history_last_failure` (a safe code).
  `next_history_review_at` stays the 24/48/72h checkpoint. A watch in backoff is
  not eligible; a successful check clears the state. A rate limit also ends the
  run's history reads; our own spent budget backs nothing off.

Checkpoint semantics are unchanged: SUFFICIENT from 24h makes a watch
PROMOTABLE, INSUFFICIENT moves to the next checkpoint, after the last it is
DORMANT. No ORBIT prerequisite exists — a watch whose first ORBIT review was
skipped as stale is promotable exactly like any other, and a TradeCase runs its
own ORBIT.

## Visibility

Scout runs record `history_eligible_now`, `history_current_selected`,
`history_catchup_selected`, `history_checks`, `history_provider_requests`,
`history_backoff_set`, `history_rate_limited` and
`oldest_history_due_age_seconds` beside `watches_due_history`. The cockpit shows
history due, eligible, backing off and the oldest due age.

## Capacity

Six reads a run are at most 576 a day against about 890 newly due first
checkpoints: the CURRENT lane keeps new checkpoints close to schedule while the
CATCH-UP lane works the backlog one watch a run. Not in this change: stored-bar
reuse, and a deterministic shortcut for markets that provably cannot be
sufficient.
