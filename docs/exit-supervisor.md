# Exit supervisor: keeping PAPER exits alive for a position's whole life

> The supervisor runs the PAPER **exit job** and nothing else. It never starts
> an entry run, a scout run or a combined `--once` run, never opens a case and
> never buys. Starting it permits no trade. It is **not activated** anywhere:
> see [Status](#status).

## Why it exists

In split operation (`PAPER_ENTRY_REQUIRES_EXIT_JOB=true`) every BUY needs
recent evidence that the exit job works: a successful, ready `--exits-once`
run that finished no more than `PAPER_EXIT_LIVENESS_MAX_AGE_SECONDS` (180 s)
ago. Without that evidence every BUY is refused with `EXIT_JOB_STALE`. An early
position (`EARLY_PAPER_EXIT_V1`) may stay open for up to 72 hours, though.
Stop-loss, trailing stop, liquidity exit and time exit only fire while exit
runs keep happening. The supervisor starts one exit run per minute for as long
as it is running, and reports loudly when it cannot.

## What it is

| Part | Where | What it does |
|---|---|---|
| launchd agent | `ops/exit-supervisor/com.rh-agents.exit-supervisor.plist.template` | `StartInterval` 60 s, `RunAtLoad`, `ProcessType` Standard. Runs from the runtime worktree on `main` only. |
| wrapper | `ops/exit-supervisor/run-exit-supervisor.sh` | Branch and clean-tree check (exit 78), bounded log, then `python -m src.supervisor.main tick`. |
| tick | `backend/src/supervisor/tick.py` | Starts `python -m src.runner.main --exits-once` as a child in its own process group. Hard wall clock of 85 s, then SIGTERM, then SIGKILL after 5 s. Classifies the child's JSON and records the result in the state file. |
| health | `python -m src.supervisor.main health` / `health.sh` | Read-only verdict: `HEALTHY`, `DEGRADED`, `CRITICAL` or `STOPPED`. |
| guards | `stop-check`, `deploy-check` | Read-only. The operator scripts refuse to act unless the guard allows it. |
| operator scripts | `install.sh`, `start.sh`, `stop.sh`, `uninstall.sh` | Separate actions. Installing starts nothing. |

The supervisor reuses existing mechanisms and adds none of its own:

- the `PAPER_EXIT_JOB` advisory lock, so there is never a second exit run;
- `paper_job_runs` together with `assess_exit_run` and `judge_exit_liveness`, so health reads the same rows a BUY reads;
- the entry halts and the durable pause, for the stop guard;
- the runner's exit codes and JSON summary.

It adds no table, no migration, no setting and no trading logic.

### Timing

| Bound | Value | Source |
|---|---|---|
| tick interval | 60 s | `INTERVAL_SECONDS`, plist `StartInterval` |
| exit run, in-process | ≤ 60 s | `PAPER_EXIT_RUN_MAX_SECONDS`. A tick refuses to start a child when this is set higher (`SUPERVISOR_EXIT_RUN_BOUND_EXCEEDED`). |
| child hard kill | 85 s | the run bound, plus two 5 s record writes, plus start-up. It stays below the gate's 90 s running grace, so a killed run's `RUNNING` row reads as `EXIT_JOB_INCOMPLETE`. |
| liveness window | 180 s | `PAPER_EXIT_LIVENESS_MAX_AGE_SECONDS`. A tick refuses anything below 120 s (`SUPERVISOR_LIVENESS_WINDOW_TOO_SHORT`). |
| missed-tick gap | > 150 s | `TICK_GAP_DETECTED`, raised for sleep/wake, a stalled job or a reboot |
| heartbeat stale | > 180 s | `SUPERVISOR_HEARTBEAT_STALE` |

There are no parallel exit runs, for three reasons. launchd never starts a job that is still running. The tick kills its child at the hard bound. Any other `--exits-once`, including a manual one, finds the advisory lock held and ends as `ALREADY_RUNNING`. The tick records that outcome as skipped: it is neither a success nor a failure.

## Prerequisites

- Runtime worktree `/Volumes/Coding/rh-agents.worktrees/runtime-main` on `main`, clean, at the deployed SHA, with the schema at Alembic head.
- The runtime `.env` permits the exit job: `PAPER_RUNNER_ENABLED=true`, `PAPER_RUNNER_MARKET_ACQUISITION_ENABLED=true`, and the exit policies for every class that can be held (`EARLY_PAPER_EXIT_ENABLED=true` for early positions).
- Check before every start: `python -m src.runner.main --exits-once` run manually returns `exit_ready=true`.
- `uv` installed. The `codex` CLI is optional; it is put on launchd's `PATH` when found at install time.
- The host qualifies (see [Host](#host-suitability)) and an alert path exists (see [Alerting](#alerting)). **Neither is the case today.**

## Install (starts nothing)

```bash
cd /Volumes/Coding/rh-agents.worktrees/runtime-main
ops/exit-supervisor/install.sh --dry-run   # inspect the rendered plist
ops/exit-supervisor/install.sh             # refuses unless on main and not loaded
```

`install.sh` marks the service disabled (`launchctl disable`) **before** it writes the plist to `~/Library/LaunchAgents`. Neither the install nor any later login loads it. It never calls `enable`, `bootstrap` or `kickstart`.

## Start (explicit, audited)

```bash
ops/exit-supervisor/start.sh --confirm --reason FIRST_EARLY_PAPER_RUN
```

`start.sh` needs `--confirm` and a reason code. It writes an audit line, then runs `launchctl enable` and `bootstrap`. The first tick runs at once, then one every 60 s. After every later login, launchd loads the agent again until `stop.sh` disables it.

Starting the supervisor changes no configuration and starts no entry job. The entry job (`--entries-once`) is a separate operator action. It is permitted only once health reports `HEALTHY`.

## Health check

```bash
ops/exit-supervisor/health.sh; echo "exit=$?"   # 0 HEALTHY 1 DEGRADED 2 CRITICAL 3 STOPPED
```

The health check prints one JSON object. Its fields:

- `status` and `reasons`
- `service` (loaded, running, last exit status)
- `open_positions` and `positions` (`normal`, `early`, `unknown`)
- `exit_liveness` (`READY` or the gate's reason) and `exit_liveness_age_seconds`
- `last_exit_run` and `last_ready_exit_run_finished_at`
- `entry_blocked` (manual halt, auto halt, paused)
- `supervisor`: the last tick, the last success, `consecutive_failures`, the last result and the last failure (both as codes and categories), the in-progress marker, the last gap and the warnings

| Status | Meaning |
|---|---|
| `CRITICAL` | A position may be held and nothing demonstrably watches it. Any of these: open positions with stale exit evidence; open positions with the service not running; an open position whose policy class the last ready run did not cover; a position no exit policy closes; an unreadable database, so positions are unknown; a stale heartbeat with open positions. |
| `STOPPED` | The service is not loaded and nothing is held. This is the intended idle state. Every BUY already fails closed on stale evidence. |
| `DEGRADED` | Something else is wrong. Any of these: consecutive failures; a non-zero last exit; stale evidence with no position held (entries blocked); a missing or unreadable state file; an abandoned tick; `TICK_GAP_DETECTED`; `CODEX_NOT_ON_PATH`; a service status that cannot be read. |
| `HEALTHY` | All checks pass. |

Failures never read green. A failed tick increments `consecutive_failures`. Only a ready run resets the count.

Failure categories in `last_failure.category`:

| Category | Meaning |
|---|---|
| `DATABASE` | The database could not be reached. |
| `PROVIDER` | Acquisition incomplete or a mark unknown: the market provider. |
| `EXECUTION_OR_RPC` | An exit attempt (the ATLAS chain read over RPC, the pre-exit refresh, or the booking) failed, timed out, was blocked or left unknown. The exit job cannot tell an RPC outage from a refused sale. |
| `TIMEOUT` | The run hit its own budget or the hard kill. |
| `CRASH` | The child died on a signal or could not start. |
| `CONFIGURATION` | The configuration was refused. |
| `COVERAGE` | An open position has no configured exit policy. |
| `SYSTEM_STOPPED` | The pause or the kill switch was in force. |

With **no** open position the exit job makes no provider or RPC call. A provider or RPC outage therefore becomes visible only once something is held.

## Alerting

**No alert path is configured on this host.** The application now has an alert pass (`python -m src.supervisor.main alert`, Telegram transport, deduplicated, persisted) and a dead-man heartbeat; see [the Linux notes](exit-supervisor-linux.md#alerting). On macOS neither is scheduled, and the Telegram hook on this machine (`@rhplatform_bot`) only reports Claude Code turns. Health is pull-only here: `health.sh`, or its exit code for any monitor. A `CRITICAL` nobody sees protects nothing, so **a missing alert path is an activation blocker**.

## Logs

The supervisor writes to four files:

- `~/Library/Logs/rh-agents/exit-supervisor.log`: one start line, one tick JSON (codes only) and one exit line per tick. It rotates at 5 MB and keeps three generations.
- `~/Library/Logs/rh-agents/exit-supervisor.launchd.log`: launchd's stdout and stderr.
- `~/.local/state/rh-agents/exit-supervisor/state.json` (0600): the tick record health reads.
- `~/.local/state/rh-agents/exit-supervisor/audit.log` (0600): one JSON line per start, stop and emergency stop, recording the reason, the actor, the status and the open positions.

The child's stderr is counted and never stored. Its stdout is reduced to codes. No URL, key or provider message reaches any of these files.

## Restart, sleep and host failure

- **Tick or child crash:** the next tick runs 60 s later. If a tick died mid-run, the next tick records `PREVIOUS_TICK_ABANDONED`. A killed child's unfinished row reads as `EXIT_JOB_INCOMPLETE` once it is older than 90 s.
- **Sleep:** no tick runs while the host sleeps. On wake, launchd fires once. Until that tick finishes, evidence is stale: `CRITICAL` with positions open, and every BUY is refused. The tick then records `TICK_GAP_DETECTED`.
- **Reboot:** the LaunchAgent loads again only after the user logs in. With FileVault on and no auto-login, nothing runs after an unattended reboot until somebody logs in. Health reports `CRITICAL` with positions open.
- **Recovery after a host failure:**
  1. Log in.
  2. Run `health.sh`. If the service is not loaded, run `start.sh`.
  3. Wait for a `READY` tick.
  4. Re-check `HEALTHY`.
  5. Read `exit-supervisor.log` and the open positions. Missed exits are not replayed: the next tick judges each position at its current mark.

## Stopping

**Regular stop:**

```bash
python -m src.runner.control --halt-entries --reason EXIT_SUPERVISOR_STOP   # block entries first
ops/exit-supervisor/stop.sh --reason BOOK_EMPTY
```

The regular stop is refused (`stop-check`) unless all of these hold:

- no position is open, and positions are known;
- no exit run is going and no tick is running;
- no entry run is going;
- a durable entry block is in force: the manual halt, the auto halt or the pause.

`stop.sh` then writes an audit line, runs `bootout` and marks the service disabled, so a login does not bring it back.

**Emergency stop:**

```bash
ops/exit-supervisor/stop.sh --emergency --reason HOST_FAILING --i-understand-exit-monitoring-stops
```

The emergency stop is not guarded and works even with positions open. It prints a warning and writes an audit line; it still stops if the audit cannot be written. Then it runs `bootout` and `disable`. It **sells nothing**: nothing liquidates automatically, and open positions stay unwatched until the supervisor is started again.

`uninstall.sh` removes the plist. It is refused while the agent is loaded. Logs, state and the audit log are kept.

## Deploys

A code deploy into the runtime worktree (`git pull --ff-only`, `uv sync`, migrations) changes the code the next tick runs. The schema may lag that code until the migration is applied. So:

```bash
ops/exit-supervisor/deploy-check.sh   # 0 allowed, 2 refused
```

`deploy-check` refuses while the supervisor is loaded, while a position is open (or positions are unknown), while an exit or entry run is recorded as going, or while any runner or tick process is alive (`pgrep`). The order is: book empty, entries halted, `stop.sh`, deploy, migrate, verify, `start.sh`. If a tick runs against a worktree that is not on `main` or is modified, the wrapper refuses it (exit 78). Such a tick writes nothing, so health turns `DEGRADED`, or `CRITICAL` with positions open.

The same gate exists for Linux (`ops/exit-supervisor/linux/deploy-check.sh`); the runbook covering both hosts is in [the Linux notes](exit-supervisor-linux.md#deploy-gate).

**Still missing (specified, not built):** there is no deploy script. The runtime deploy is a manual runbook. That runbook, and any future deploy tooling, must call `deploy-check.sh` first and abort on a non-zero exit. The scout's `install.sh` unloads only `com.rh-agents.scout` and never touches this agent.

## Host suitability

Assessed read-only on 2026-10-13. No setting was changed.

| Fact | Finding | Consequence |
|---|---|---|
| Machine | MacBook Pro (laptop), on AC, battery present | Closing the lid or losing AC changes power behaviour. |
| Idle sleep | `sleep 0`; currently also held off by assertions (`caffeinate`, `sharingd`) | Lid close and low battery still sleep the machine. Sleep is detected (`TICK_GAP_DETECTED`, stale evidence), not prevented. |
| FileVault | On, no auto-login | After any reboot (update, panic, power loss after battery drain), nothing runs until a login. A LaunchDaemon would not help: the volume stays locked until a login. |
| Auto-restart after power loss | Not available on this laptop | Recovery requires a person. |
| Uptime | 22 days | Stable so far. This is evidence, not a guarantee. |
| Mechanism | A LaunchAgent in the user's GUI session, like the scout | This is the right mechanism here. It has the user's `uv`, Codex login and `.env`. A LaunchDaemon would need root and a separate environment, and would gain nothing under FileVault. |

**Verdict:** this host cannot guarantee 72 hours of unattended exit supervision. Activation stays **NO_GO** until an operator explicitly accepts one of these:

- attended operation: lid open, AC, automatic restarts for updates deferred, health watched;
- or a host that boots unattended, such as the Linux VPS target with the systemd units in `ops/exit-supervisor/linux` ([Linux notes](exit-supervisor-linux.md)).

In either case a working alert path is required as well.

## Status

| Item | State |
|---|---|
| Implemented | Tick, health, stop and deploy guards, operator scripts, plist template. |
| Tested | Unit and process tests (real hung, crashing and orphaning children); script tests against stand-in `launchctl` and `uv`; database reads on SQLite and on disposable PostgreSQL schemas. |
| Not operationally ready | No alert path. The host is not suitable for unattended 72 h operation. Deploy tooling does not call `deploy-check`. The provider and RPC path are unproven until something is held. |
| Activation approved | **No.** Nothing is installed, loaded or started on the runtime host. |
