# Early discovery: operating the scout and the cockpit

> A scout watch is **not** a trade recommendation. PROMOTABLE is **not** a buy
> approval. The cockpit is read-only. It shows what the scout recorded and
> cannot trade, approve, sign or change a setting.

## Cadence and ORBIT budget

Every watch is owed six ORBIT reviews: T+0, 1h, 3h, 6h, 12h and 24h. A run that
creates `W` new watches therefore adds `6·W` reviews to the steady-state
workload. Unless `EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_RUN ≥ 6 ×
EARLY_SCOUT_MAX_NEW_WATCHES_PER_RUN`, the due queue grows without bound.

| Setting | Default | Why |
|---|---|---|
| cadence | every 15 min | The shortest checkpoint gap is 1h, so a 15-minute run is late by at most 15 minutes per checkpoint. |
| `EARLY_SCOUT_MAX_NEW_WATCHES_PER_RUN` | 1 | 96 new watches a day, taken in the provider's `new_pools` order. There is no ranking of any kind. |
| `EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_RUN` | 8 | 6 cover the steady state and 2 drain a backlog. |
| model calls | ≤ 8 per run, ≤ 768 per day | This is a hard bound. There is at most one review per watch per run, and missed checkpoints are never replayed. |

To watch more new pools per run, raise both settings together and keep the
ratio at 1:6 or higher. The cost grows linearly with it. The run summary and
the cockpit show whether the scout keeps up:

- `orbit_backlog_before` / `orbit_backlog_after` are the due reviews when the
  review phase began and ended.
- `oldest_orbit_due_age_seconds` is the age of the longest-waiting due review.
- `new_watches_without_orbit_assessment` is the number of watches that have
  never been reviewed.

The due queue itself stays neutral: it is ordered by `next_orbit_review_at`,
then `first_seen_at`, then `pair_id`. It is never ordered by liquidity, volume,
market cap or classification.

## Running the scout on a schedule (macOS launchd)

```
ops/scout/install.sh --dry-run   # print the rendered plist, change nothing
ops/scout/install.sh             # install and load the per-user agent
ops/scout/uninstall.sh           # unload and remove it (logs stay)
```

- The agent runs only `python -m src.runner.main --scout-once` from `backend/`,
  every 900 seconds. It never runs the full PAPER run.
- **No overlap.** launchd never starts a job again while the previous run is
  still going. The scout also holds a PostgreSQL advisory lock for the whole
  run, so a manual run and a scheduled one cannot run together either. The
  second one reports `ALREADY_RUNNING` and does nothing.
- **Configuration.** The agent reads the same root `.env` a manual run reads.
  It must set `EARLY_SCOUT_ENABLED=true`, `MARKET_PROVIDER=geckoterminal`,
  `MARKET_CHAINS=bsc` and `REASONING_PROVIDER=anthropic`. Put secrets such as
  `ANTHROPIC_API_KEY` in the `.env` (gitignored) or in
  `~/.config/rh-agents/scout.env` (`chmod 600`, outside the repository). The
  wrapper exports that file first. Nothing secret is committed, and none is
  written into the plist.
- **Logs.** `~/Library/Logs/rh-agents/scout.log` holds the run summary JSON
  plus start and exit lines. It rotates at 5 MB and keeps 3 generations.
  launchd's own output goes to `scout.launchd.log`. A failed run keeps its exit
  code in both.

## Run history

Every run that reaches the scout writes exactly one terminal row to
`scout_runs` (migration `0013`):

- `COMPLETED` is an ordinary run.
- `STOPPED` means a durable system stop was in force and nothing was asked.
- `FAILED` means a technical fault; its typed codes are in `errors`.

A row is written once, at the end, so there are no half-finished rows. Runs
refused before they start (scout disabled, or another run holds the lock) are
not runs and leave no row. History starts with the first run after `0013`:
nothing earlier is reconstructed.

Rates are computed on read: identity acceptance is `valid_markets / discovered`
and watch creation is `watches_created / valid_markets`. Both are `null` (shown
as N/A) when the denominator is zero, never 0%.

## The cockpit

Start the backend and the frontend locally:

```
cd backend && uv run uvicorn src.api.main:app --port 8000
cd frontend && npm run dev        # http://127.0.0.1:3000/scout
```

The page shows:

- scout status and coverage;
- the watch list, filterable by all, very young (under 6 hours), watching,
  promotable, dormant or retired;
- a watch's detail with its ORBIT timeline, where each checkpoint is
  `ASSESSED`, `FAILED`, `COALESCED` (covered by a later review), `DUE`,
  `PENDING` or `NOT_SCHEDULED`;
- the run history;
- the booked paper ledger.

The watch list is ordered newest-discovery-first. That is a display order only
and does not change what the scout works on next.

The page reads through `/api/cockpit/...`, a GET-only proxy with a fixed path
allowlist. The allowed backend read APIs are:

- `GET /api/scout/overview`
- `GET /api/scout/watches`
- `GET /api/scout/watches/{id}`
- `GET /api/scout/watches/{id}/assessments`
- `GET /api/scout/runs`
- `GET /api/paper/portfolio`
- the existing read-only `/api/trade-cases/{id}` views, used to follow a
  promoted watch to its case

Scout assessments are discovery history. A TradeCase formed from a PROMOTABLE
watch runs its own ORBIT review and never uses the scout's assessments as
evidence. The paper section shows booked ledger rows only. Without positions,
fills or P&L snapshots it says so and shows no example values.
