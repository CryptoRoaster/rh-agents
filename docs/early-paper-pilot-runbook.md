# Early PAPER pilot: runtime preparation runbook (DRAFT — nothing executed)

A reproducible order of work for a **controlled early PAPER pilot** on
Robinhood Chain. Every step that changes the runtime, a database, a scheduler
or calls a provider needs its own explicit approval at that moment. It extends
the ops window in `docs/paper-minimum-release-gate.md` and the readiness gates
in `docs/early-paper-readiness.md`.

**The pilot is not releasable today.** Exit continuity is not established: the
current Mac gives no guaranteed 72-hour exit monitoring (sleep, FileVault
restarts, updates), and the existing VPS was assessed `VPS_NO_GO` (shared, no
root, memory tight, swap in use). An early position lives up to 72 hours;
without a host that keeps the exit job running for that long, no entry may be
opened. Step 8 is therefore a hard gate.

## 0. Ground rules

- Runtime worktree `rh-agents.worktrees/runtime-main` (currently `cf25664`).
- Never print a secret; confirm settings by name only.
- Stop any step on the first `NO`; the runtime stays as it was.
- Exit job before entry job, always; entries only in early-only mode.

## 1. Runtime preflight (read-only)

1. Record the runtime commit, every launchd/systemd job and running
   `src.runner.main` / `src.scout` process.
2. `alembic current` on the runtime database (expected `0024`), account row,
   `paused`, entry halts, open positions with their policy class.
3. `python -m src.runner.main --preflight` with the current code.
*Go:* inventory complete, no open position without an exit policy.

## 2. Backup

1. Stop every job (respect the scout-unload gate: never stop a scout mid-run).
2. `pg_dump -Fc` of the runtime database to an encrypted location outside the
   repository; restore into a scratch database; `alembic upgrade head` and
   `alembic check` on the scratch copy; drop it.
*Go:* backup restorable, migrations clean on the copy.

## 3. Migrations 0025 and 0026

Deploy the reviewed main commit into the runtime worktree, then:

1. `alembic upgrade 0025` — `scout_runs` gains three discovery counters (0).
2. `alembic upgrade 0026` — empty `v4_pool_index_coverage` / `v4_pool_index_pools`.
3. `alembic current` = `0026`; `--preflight` exit 0.
*Rollback:* both are additive; downgrade only while the index is empty,
otherwise restore the backup with every job stopped.

## 4. Pool index backfill (RPC budget required)

`python -m src.agents.atlas.v4.index_job --chain robinhood --max-requests 800`
repeated until it exits `0` (`3` = partial, resume; `1` = failed, investigate).

| | Reads |
|---|---|
| deployment search | ≈ 29 |
| backfill at 100 000-block windows | ≈ 3 100 at ~77 M blocks, + ≈ 40 per million blocks since |
| worst case at 25 000-block windows | ≈ 12 400, + ≈ 160 per million blocks |
| **approval to request** | **4 000** (13 000 if the provider caps windows) |

The chain grows by ≈ 0.86 M blocks a day; the estimate must be recomputed with
the real head at approval time (`docs/atlas-v4-pool-index.md`).

## 5. Verify the index is complete

1. `v4_pool_index_coverage.through_block` within the confirmation lag of head;
   `deployment_block` has PoolManager code and its predecessor none.
2. Pool count and a random sample of 20 pools cross-checked against a second,
   independent RPC endpoint (≈ 40 reads).
3. Only then `ATLAS_V4_POOL_INDEX_ENABLED=true`.

## 6. FeeSplitter verification (13 reads, `docs/fee-splitter-verification.md`)

Pin a block; `eth_getCode` + `getSplits()` for the four registered splitters;
`eth_getCode` for the recipients. Expected outcome: v3.3.0 templates and
splits confirmed; token-side fees stay **uncreditable** (permissionless
compounder claims), so traded official launches remain UNKNOWN. A pilot
candidate is therefore a launch with no token-side fee owed yet.

## 7. Scout configuration

`EARLY_SCOUT_MAX_DISCOVERY_POOLS=20`, `EARLY_SCOUT_MAX_NEW_WATCHES_PER_RUN=40`,
`EARLY_SCOUT_DISCOVERY_PAGE2_ENABLED=true`, `EARLY_SCOUT_MAX_DISCOVERY_PAGE2_PER_DAY=72`,
`EARLY_SCOUT_V4_PREFILTER_ENABLED=true`, ORBIT ≤ 10 per run / 96 per day;
reinstall with `RH_AGENTS_SCOUT_INTERVAL_SECONDS=300` (offline-verified:
`tests/scout/test_target_profile.py`).

## 8. Exit supervisor and alerting (hard gate)

Required before any entry:

- A host that runs the exit job continuously for at least 72 hours beyond the
  last possible entry, survives reboots, and keeps the clock and network:
  a dedicated Linux host with root and systemd (units in
  `ops/exit-supervisor/linux`), not the Mac, not the `VPS_NO_GO` host.
- Exit job interval well under `PAPER_EXIT_LIVENESS_MAX_AGE_SECONDS` (180 s);
  `PAPER_ENTRY_REQUIRES_EXIT_JOB=true`.
- The runtime PostgreSQL instance and the network path to the RPC and market
  providers fall under the same 72-hour condition: a Linux exit host is of no
  use while the database lives on the Mac.
- `ops/exit-supervisor/linux/install.sh` defaults to `--host-alias rh-vps`,
  which is the `VPS_NO_GO` host: always pass the target host explicitly.
- Alerting through the rh-agents Telegram bot (single chat) on any exit run
  that is not ready -- every `ExitHealth` reason other than ready
  (`backend/src/orchestration/jobruns/models.py`), among them `EXIT_BLOCKED`,
  `EXIT_ATTEMPT_FAILED` (also for a failed judgement, #111),
  `EXIT_OUTCOME_UNKNOWN`, `EXIT_ATTEMPT_TIMED_OUT`, `EXIT_PLAN_INCOMPLETE`,
  `EXIT_BACKLOG`, `EXIT_MARK_UNKNOWN`, `EXIT_ACQUISITION_INCOMPLETE`,
  `EXIT_JOB_TECHNICAL_FAILURE`, `EXIT_JOB_SYSTEM_STOPPED` and
  `EXIT_POLICY_COVERAGE_MISSING` -- and a dead-man alert when no exit run
  finished within twice its interval.
- A tested manual stop
  (`python -m src.runner.control --halt-entries --reason PILOT_STOP_TEST`).
- Scheduler units for the index job and the entry job (`--entries-once`) do
  not exist in `ops/` yet; they are built and reviewed before this step.
*Go:* 24 hours of exit runs on the target host, every one `exit_ready=true`,
one deliberately failed run alerted within its interval.

## 9. Controlled early PAPER pilot

- `PAPER_RUNNER_ENABLED=true`, `EARLY_SCOUT_ENABLED=true`,
  `ATLAS_FUNDING_GRAPH_ENABLED=true` (all required by the early entry flag's
  validation), `PAPER_EARLY_ONLY=true`, `PRE_VECTOR_EARLY_ENTRY_ENABLED=true`,
  `EARLY_PAPER_EXIT_ENABLED=true`, `ATLAS_V4_POOL_CONTROL_ENABLED=true`,
  `ATLAS_V4_POOL_INDEX_ENABLED=true`; strategy limits unchanged (10 USD per
  entry, 5 open, 50 USD exposure, one entry per run).
- Index job every 5 minutes; scout every 5 minutes; exit job continuously;
  entry job only after the exit job has shown `exit_ready=true`.
- One observed entry run at a time for the first day.

### Stop criteria (stop entries at once, keep exits running)

- any exit run not ready twice in a row, or one `OUTCOME_UNKNOWN`;
- a missed dead-man alert or an unreachable exit host;
- an LP-control PASS on anything but a 32-byte V4 pool, or a BUY outside
  early-only;
- the daily loss limit (automatic entry halt) or any system pause;
- index coverage behind head by more than one hour, or an index run `FAILED`
  twice in a row;
- provider budget for the day reached (below).

## 10. Immediate stop and rollback

1. `python -m src.runner.control --halt-entries --reason PILOT_STOP`; unload
   the entry job. Exits keep running.
2. If exits themselves misbehave: the durable system pause (set by SENTINEL)
   or `COMMANDER_KILL_SWITCH=true` in the runtime settings stop **exits as
   well as entries**; use them only when continuing to sell is itself unsafe.
   Lifting a pause follows `docs/paper-minimum-release-gate.md`. Open positions
   are PAPER only.
3. Code rollback: redeploy the previous commit; the new tables stay. Schema
   rollback only with every job stopped, from the backup of step 2.

## Load and cost

| Source | Steady load | Bounded by |
|---|---|---|
| Robinhood RPC: index increments | ≈ 7 reads / 5 min ≈ 2 000 / day | `--max-requests` per call |
| Robinhood RPC: ATLAS per early candidate | ≤ 240 census reads + contract reads | census `max_requests`; candidates per run (≤ 5) |
| Robinhood RPC: exit reads | a few reads per triggered position | `paper_exit_max_attempts_per_run` (5) |
| GeckoTerminal | ≈ 288 / day, ≈ 345 with page 2 | page-2 cap 72 / day |
| Holder provider | one page per ATLAS read | `atlas_holder_max_pages` (1) |
| ORBIT model | ≤ 96 reviews / day | per-run and per-day caps |

Cost risk lies in the RPC plan (compute units per `eth_getLogs` and
`eth_call`), the holder provider plan and the model budget; every source is
capped by a setting, and the daily figures above are the planning ceiling to
approve. External services required: Robinhood RPC (and a second endpoint for
verification), GeckoTerminal, the holder provider, the ORBIT model provider,
the Telegram bot, and the exit host.
