# Early discovery: operating the scout and the cockpit

> A scout watch is **not** a trade recommendation. PROMOTABLE is **not** a buy
> approval. The cockpit is read-only. It shows what the scout recorded and
> cannot trade, approve, sign or change a setting.

## Cadence and ORBIT budget

**Discovery capacity is intentionally decoupled from model-review capacity.**
Discovery is the cheap input. Every valid pool a run finds, up to its own bound,
becomes a watch, whether or not ORBIT can review it right away. A watch waits
in the deterministic due queue. If it is reviewed late, its missed checkpoints
coalesce into one review. Nothing is dropped, retired or made dormant for lack
of model budget.

Paid ORBIT calls are bounded twice: per run and per UTC day.

| Setting | Default | Why |
|---|---|---|
| cadence | every 15 min | Discovery every 15 minutes, so a young pool is seen early. |
| `EARLY_SCOUT_MAX_NEW_WATCHES_PER_RUN` | 10 | Every valid pool on the `new_pools` page becomes a watch. No ranking, no filter. |
| `EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_RUN` | 4 | Four calls at the observed 7-10 s latency (60 s timeout worst case) fit well inside one 15-minute run. |
| `EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_DAY` | 96 | The hard daily cost bound: on average one review per run. A run may burst up to its per-run bound while the day has budget. |
| `EARLY_SCOUT_MAX_REFRESH_MARKETS_PER_RUN` | 4 | A stale due watch needs a fresh reading before review. Refreshes are batched into one `pools/multi` request per chain. A watch that is already fresh costs nothing. |
| GeckoTerminal requests | ≤ 4 per normal run | One network lookup, one `new_pools` read, **one** batched refresh shared by the reviews and the history check, and at most one history read. That leaves one request of headroom under the unchanged default transport budget of 5 (`GECKOTERMINAL_MAX_REQUESTS`). The scout never loosens that operator setting. A run that meets it stops asking and reports a provider failure. |

The daily bound is a **hard** bound held by durable reservations. Before every
scout ORBIT call, one slot is reserved and committed in
`scout_orbit_reservations`. The order is:

1. read the UTC day's usage under a transaction advisory lock;
2. refuse if the cap is reached;
3. reserve the slot;
4. claim the checkpoint;
5. call the model;
6. write the assessment;
7. settle the reservation as COMPLETED or FAILED.

RESERVED, COMPLETED and FAILED all count. A process that dies mid-call leaves
its RESERVED slot counted. That is conservative by design: a slot may be spent
for a call that never reached the provider, never the other way round. If a run
dies after reserving but before claiming, no call was made, and the next run
reuses that slot instead of counting it twice.

If the reservation cannot be written, or the day's usage cannot be read, no
model is asked. The budget resets at 00:00 UTC.

A 15-minute schedule therefore does **not** guarantee six assessments per
watch. It guarantees:

- discovery every 15 minutes;
- ORBIT bounded per run and per day;
- checkpoints coalesced when there is a backlog;
- backlog and coverage visible in the cockpit.

Each run summary and run row reports:

- `orbit_backlog_before` and `orbit_backlog_after`;
- `oldest_orbit_due_age_seconds`;
- `new_watches_without_orbit_assessment`;
- `orbit_daily_budget` and `orbit_daily_used_before` / `_after`, with the
  remaining budget.

The cockpit shows "ORBIT budget today: used / cap", or "Daily ORBIT budget
reached" once the cap is hit. The latter is a normal state, not an error:
discovery continues and due reviews wait. The cockpit shows calls and token
counts, never an estimated dollar amount.

The due queue stays neutral: it is ordered by `next_orbit_review_at`, then
`first_seen_at`, then `pair_id`. It is never ordered by liquidity, volume,
market cap or classification.

## Running the scout on a schedule (macOS launchd)

```
ops/scout/install.sh --dry-run   # print the rendered plist, change nothing
ops/scout/install.sh             # install and load the per-user agent
ops/scout/uninstall.sh           # unload and remove it (logs stay)
```

- **Runtime worktree, never the development checkout.** The agent runs the
  code of the worktree it was installed from. Install it from a dedicated
  worktree that tracks `main`, for example:

  ```bash
  git worktree add /Volumes/Coding/rh-agents.worktrees/runtime-main main
  cp -p .env /Volumes/Coding/rh-agents.worktrees/runtime-main/.env
  cd /Volumes/Coding/rh-agents.worktrees/runtime-main/backend
  uv sync --locked --python 3.13 && uv run alembic upgrade head
  ../ops/scout/install.sh
  ```

  The development worktree may then switch branches freely; git keeps `main`
  checked out in the runtime worktree only, so develop from `origin/main`.
  Two guards make this hold: `install.sh` refuses to install from any branch
  but `main`, and the plist sets `RH_AGENTS_SCOUT_REQUIRE_BRANCH=main`, so
  `run-scout.sh` refuses (exit 78, `RUNTIME_NOT_ON_MAIN` or
  `RUNTIME_WORKTREE_MODIFIED`) to run a worktree on another branch or with
  tracked changes. To update the runtime: `git pull --ff-only`, `uv sync
  --locked`, then `alembic upgrade head` — migrate before a release that adds
  columns reaches the scheduler.
- The agent runs only `python -m src.runner.main --scout-once` from `backend/`,
  every 900 seconds. It never runs the full PAPER run.
- **No overlap.** launchd never starts a job again while the previous run is
  still going. The scout also holds a PostgreSQL advisory lock for the whole
  run, so a manual run and a scheduled one cannot run together either. The
  second one reports `ALREADY_RUNNING` and does nothing.
- **Configuration.** The agent reads the same root `.env` a manual run reads.
  It must set `EARLY_SCOUT_ENABLED=true`, `MARKET_PROVIDER=geckoterminal`,
  `MARKET_CHAINS=bsc` and a reasoning provider. Put secrets such as
  `ANTHROPIC_API_KEY` in the `.env` (gitignored) or in
  `~/.config/rh-agents/scout.env` (`chmod 600`, outside the repository). The
  wrapper exports that file first. Nothing secret is committed, and none is
  written into the plist.
- **ChatGPT subscription instead of an API key.** `REASONING_PROVIDER=codex`
  asks GPT-5.5 through the local Codex CLI and its ChatGPT login; no key is
  configured anywhere. launchd's PATH does not contain the CLI, so the scout
  env must name it, for example `CODEX_EXECUTABLE=/opt/homebrew/bin/codex`.
  The login stays in `~/.codex` (or `CODEX_HOME`); every call copies
  `auth.json` into a throwaway isolated home and runs behind the harness's
  Seatbelt profile and release gates. A refused gate is a failed review, never
  an unsandboxed one. The daily ORBIT cap applies unchanged.
- **Codex timeout.** Put `REASONING_TIMEOUT_SECONDS=120` in the scout env, not
  in the root `.env`: it is a local override for the scout process, and the
  global default stays 60 s for every other caller. The 120 s are the whole
  call. The preflight probes (CLI version, login, sandbox boundary) share it
  and must leave at least 15 s (a 10 s turn minimum plus its 5 s cleanup
  reserve); the turn then gets exactly what is left. There is no separate
  preflight budget on top, and a probe that runs out of time fails its gate
  (`CODEX_PREFLIGHT_TOO_SLOW`), it never passes one.
- **Process class.** The agent runs as `ProcessType=Standard`. It used to be
  `Background`, which lets macOS throttle CPU, I/O and network for the whole
  process tree, Codex included. Measured on the development machine (Codex
  preflight only, 5 runs each): Standard median 0.49 s, Background median
  5.6 s, and under the full background policy (`taskpolicy -b`) the preflight
  refused its own release gates in 3 of 5 runs, taking up to 94 s. Standard
  adds no priority boost; it only stops the throttling.
- **Why a review failed.** A failed review keeps its category
  (`failure_reason`, such as `PROVIDER_NOT_CONFIGURED`) and the provider's
  reason (`failure_reason_code`, such as `CODEX_GATE_NETWORK_EGRESS`,
  `CODEX_PREFLIGHT_TOO_SLOW` or `CODEX_DEADLINE_EXCEEDED`). Each run also
  records its failures counted by provider, category and code
  (`model_failure_reasons`), which the run history shows beside the failure
  count. Codes only: a reason that is not a plain code is stored as
  `UNCLASSIFIED`, so no path, message, stderr or login detail is kept.
- **Effort.** A review records the effort it asked for (`reasoning_effort`)
  and, separately, the effort the provider reported (`reported_effort`). Codex
  0.153.4 reports none, so its reviews show `high (requested, not reported)`.
  A requested value is never shown as a reported one. Rows from before
  migration 0015 have neither.
- **Capacity.** See [the ORBIT capacity analysis](architecture/scout-orbit-capacity.md).
- **Watch limit.** Discovery records every valid pool, but opens at most
  `EARLY_SCOUT_MAX_NEW_WATCHES_PER_RUN` watches, taking chains in turn
  (robinhood, bsc, robinhood, …) in provider order within a chain — a
  TEMPORARY NEUTRAL WATCH ALLOCATION; nothing is ranked by market size or by
  JEV. A stream the limit turns away is recorded in `discovery_stream_declines`
  (`watches_declined` in the summary) as NOT_OPENED_AS_WATCH_DUE_TO_WATCH_LIMIT:
  it is never adopted by the recovery bootstrap and never opened by a later
  discovery run, but it is still observed and still gets its JEV-0 assessment.
- **Outcome labels.** Optional, off by default (`OUTCOME_SAMPLER_ENABLED`).
  Objective returns, drawdowns and survival for watched and declined
  candidates, on the sampler's own request budget; see
  [discovery outcomes](architecture/discovery-outcomes.md) and
  `GET /api/scout/outcomes/summary`.
- **JEV shadow triage.** Optional, off by default. Every new discovery
  candidate is assessed, declined ones included; see
  [JEV-0](architecture/jev-shadow-triage.md). The watch detail shows the
  assessment of its stream under "Fast shadow assessment", marked SHADOW — NO
  TRADING EFFECT; `GET /api/scout/shadow/summary` counts all of them.
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
