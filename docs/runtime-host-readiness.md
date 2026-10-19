# Runtime host readiness: isolated Linux host, dead-man receiver, job units, database

Preparation for the controlled early PAPER pilot (2026-10-19). **Nothing here
is procured, installed, enabled or run against the runtime.** The runtime
checkout still runs `cf25664`, its database is at `0024`, no scheduler was
changed. Every step that changes a host, the runtime database, a scheduler or
calls a provider needs its own approval (`docs/early-paper-pilot-runbook.md`).

Status: **NO-GO** until a host meeting section 1 exists and has passed the
72-hour acceptance in section 7.

## 1. Trading host: binding requirements

One dedicated Linux host for exits, entries, the index, the scout **and** the
runtime PostgreSQL. Not the Mac (sleep, FileVault restarts, updates), not the
shared VPS (`VPS_NO_GO`: no root, memory tight, swap in use), and no realTORO
or other application server: nothing else runs there.

| Requirement | Minimum | Why |
|---|---|---|
| Ownership | dedicated VM or machine, root access, no other application | units, credentials and the database are system-level; a neighbour can starve or stop exits |
| OS | Debian 12 or Ubuntu 24.04 LTS, systemd ≥ 252, unattended security updates **without automatic reboot** | the units use `StateDirectory`, `LoadCredential`, `ProtectSystem=strict`; a reboot mid-pilot is a planned event |
| CPU | 2 vCPU (dedicated, not burst credits) | PostgreSQL plus one job at a time; units cap jobs at 50–100 % of one CPU |
| RAM | 4 GiB minimum, 8 GiB recommended, no swap pressure | unit ceilings (`MemoryMax`): exit tick 800 MiB, alert 400, heartbeat 400, index 400, scout 700, entry 700 = 3.4 GiB if all ran together, plus PostgreSQL (`shared_buffers` 512 MiB) |
| Disk | 40 GiB SSD, persistent, provider snapshots | runtime database today 59 MiB (measured), journal, backups before off-site copy |
| PostgreSQL | 17 from the distribution or PGDG, local Unix socket, `postgresql@17-main.service` | the job units order themselves after it; client tools of the same major (section 4) |
| Network | stable outbound HTTPS to the RPC, GeckoTerminal, Blockscout, Telegram, the model provider; outbound SSH to the dead-man receiver; **no inbound port** except SSH for the operator | a lost network is detected (section 2) but cannot be healed by software |
| Time | `systemd-timesyncd` or chrony, synchronised | liveness (180 s), heartbeat skew and freshness rules use wall time |
| Restart | provider auto-restart after host failure; every timer `WantedBy=timers.target`, `OnBootSec` | jobs resume by themselves after a boot; the tick reports `HOST_RESTART_DETECTED` |
| Availability | the exit job, the database and the network path for **72 hours beyond the last possible entry** | an early position lives up to 72 hours |
| Secrets | `.env` mode 0600 owned by the service user (database, RPC, providers, model); Telegram and heartbeat keys root-owned 0600 in `/etc/rh-agents/credentials`, given to their unit only (`LoadCredential`); Codex login in `/var/lib/rh-agents/codex` (0700) if ORBIT runs through the Codex CLI | no job sees an alert secret; `ProtectHome=yes` hides home directories from every unit |
| Monitoring | alert pass every 60 s (Telegram), heartbeat to the receiver every 60 s, disk usage below 80 % | section 2 |
| Backups | nightly `pg_dump -Fc` (same major), encrypted off-site copy, 14 days kept, restore probe weekly with `ops/db/rehearse-migration.sh` | section 4 |

Service user `rh-agents`, runtime checkout `/opt/rh-agents` on `main`
(the wrappers refuse anything else, including untracked files),
virtualenv at `backend/.venv` built with `uv sync --locked`.

## 2. Dead-man receiver: second, independent machine

The alert pass runs on the trading host and falls silent with it. A second
machine receives signed heartbeats and alarms when they stop
(`backend/src/supervisor/deadman.py`, tested by `tests/supervisor/test_deadman.py`).

| Check | Design |
|---|---|
| Independence | a different provider or at least a different region and account than the trading host; no trading secret, no database, no RPC key on it; any small always-on Linux machine |
| Receiving | SSH with a forced command: `restrict,command="python -m src.supervisor.deadman receive --store /var/lib/rh-deadman --host <alias>"` in the receiver user's `authorized_keys`; no listening service besides sshd |
| Authenticity | HMAC-SHA256 over the canonical payload with a shared key; expected host alias; `sent_at` within the skew bound and later than the last accepted one (replay- and reorder-proof, also across receiver restarts) |
| Alarm on silence | a timer runs `deadman check --store … --host <alias>` every 60 s; missing or older than `--max-age` (proposal 180 s) is CRITICAL and sent through Telegram |
| Network loss | the trading host losing its network shows up as missing heartbeats at the receiver; the receiver losing its own network fails its Telegram send, which is retried on the next pass; a heartbeat that cannot be delivered never refreshes liveness |
| Restart | the receiver's last accepted instant is persisted; after a reboot its first check judges the age of the last heartbeat, so an outage spanning the reboot still alarms |
| Keys | HMAC key on both machines (root 0600; trading host via `LoadCredential=heartbeat-key`); SSH private key and pinned `known_hosts` only on the trading host; the receiver's `authorized_keys` entry is restricted to the forced command |
| Telegram test | trading host: `start.sh --confirm --reason` sends a real `STOPPED` alert before the supervisor starts (refuses to start otherwise); receiver: `deadman check --dry-run`, then a deliberate heartbeat stop longer than `--max-age` must produce one CRITICAL message, and resuming must clear it |

To be built before activation (not done here): the receiver's own check unit
and timer, and its install script. Activation: **NO**.

## 3. Job units (`ops/jobs/linux`)

Index, scout and entry run as systemd oneshot services with 5-minute timers,
next to the existing exit supervisor (`ops/exit-supervisor/linux`, unchanged
apart from the deploy gate now also waiting for a running index job).

| Unit | Runs | Guards |
|---|---|---|
| `rh-agents-pool-index` | `python -m src.agents.atlas.v4.index_job --chain robinhood --max-requests N` (N from `install.sh --index-max-requests`, default 50, 1..2000) | clean `main`, Python present, per-job `flock`; exit 3 (`PARTIAL`) is success, the next run resumes |
| `rh-agents-scout` | `python -m src.runner.main --scout-once` | the same; the scout's own advisory lock and budgets |
| `rh-agents-entry` | `python -m src.runner.main --preflight`, then `--entries-once` | the same, plus: exit-supervisor **and** alert timers active, preflight exit 0; otherwise exit 78 and nothing runs. In the job itself `PAPER_ENTRY_REQUIRES_EXIT_JOB` and `EXIT_JOB_STALE` still gate every BUY |

Common to all three: `After=network-online.target postgresql.service`,
`User=rh-agents`, the exit supervisor's hardening, `MemoryMax`/`CPUQuota`
limits, `StandardOutput=journal` with `SyslogIdentifier=rh-agents-<job>`,
`KillSignal=SIGINT` (the runner records `RUN_INTERRUPTED`; every write is
idempotent by its key) and `KillMode=control-group` after 30 s, no
credentials. Rate limits are the code's own settings (index requests per run,
scout page-2 and ORBIT caps, exit attempts per run) plus the timers' cadence.

Order of operation: exit supervisor installed and started (alerts first, its
own `start.sh`), then `ops/jobs/linux/install.sh`, then `start.sh --job index`
(after the backfill), `--job scout`, and only then `--job entry`, which is
refused while exits are not watched and alarmed. After a boot the timers start
in the same order (`OnBootSec` exit 60 s, index 120 s, scout 180 s, entry 300 s).
`stop.sh --job all --reason CODE` stops entries first and never touches the
exit supervisor. Tested in `tests/ops/test_jobs_linux.py` (60 tests; stand-ins
for systemctl, Python and flock; mutation-checked).

## 4. Database: backup, restore probe, migrations, backfill, rollback

`ops/db/rehearse-migration.sh --source-db NAME --scratch-db NAME --out FILE --pg-bin DIR`
dumps the source (read-only), restores into a **new** scratch database,
migrates the copy to head and verifies it. It refuses a scratch name equal to
the source or without `rehearsal`/`scratch` in it, an existing scratch
database, an existing backup file, and a `pg_dump` whose major version is not
the server's (`tests/ops/test_db_rehearsal.py`).

Rehearsal on 2026-10-19, local PostgreSQL 17.11, scratch databases only:

| Step | Result |
|---|---|
| fresh schema at `0024`, account paused | 39 tables |
| `pg_dump` 16.14 from PATH against server 17 | **refused** by pg_dump itself; the script now refuses up front — on the host use `/usr/lib/postgresql/17/bin` |
| backup with pg_dump 17 | 148 652 bytes, mode 0600, 39 tables with data |
| restore into a new database | revision `0024`, paused flag preserved |
| `alembic upgrade 0025`, `0026` | head `0026`; `scout_runs` gains three counters; empty `v4_pool_index_coverage` / `_pools` |
| `alembic check` | no differences between schema and models |
| `alembic downgrade 0024`, `upgrade head` | clean both ways while the index is empty |

Runtime order (approval required): stop every job (respect the deploy gate),
run the rehearsal against the runtime database into a scratch copy, keep the
dump off-site, deploy, `alembic upgrade head`, `--preflight` exit 0.

Backfill (approval with RPC budget): `index_job --max-requests 800` repeated
until exit 0; exit 3 = budget reached, run again (resumes from the confirmed
block); exit 1 = investigate. Offline-proven: resume after a budget stop,
a reorganised tip rewound and re-read, a transport failure keeping what was
confirmed, a window whose parent moved not stored, a census refusing an index
whose tip the chain no longer has (`tests/atlas/v4/test_pool_index.py`).
Verification: coverage within the confirmation lag of head, 20 pools
cross-checked on a second RPC endpoint.

Rollback: code rollback = redeploy the previous commit (new tables stay,
unused). Schema rollback only with every job stopped: **the 0026 downgrade
drops the index tables and everything backfilled**, so it is used only while
the index is empty; otherwise restore the dump into a fresh database and
switch `DATABASE_URL` after the restore probe.

## 5. Provider and operating cost

Legend: **GEMESSEN** measured here, **BERECHNET** computed from code limits or
chain data, **GESCHÄTZT** estimated, **NICHT VERIFIZIERT** not checked
(no provider was called, no price page was relied on).

| Item | Volume | Class | Cost |
|---|---|---|---|
| Initial pool-index backfill | ≈ 3 100 reads at 100 000-block windows (+ ≈ 40 per million blocks since) | BERECHNET (`tests/atlas/v4/test_backfill_estimate.py`) | depends on the RPC plan: NICHT VERIFIZIERT |
| Backfill worst case | ≈ 12 400 reads at 25 000-block windows; approval asked 4 000 / 13 000 | BERECHNET | NICHT VERIFIZIERT |
| Index increments | ≈ 7 reads per 5 min ≈ 2 000 a day; capped by `--max-requests` 50 per run (≤ 14 400 a day) | BERECHNET | NICHT VERIFIZIERT |
| Index verification, FeeSplitter | ≈ 40 reads once; 13 reads once | BERECHNET | negligible |
| ATLAS per early candidate | ≤ 240 census reads + contract reads; ≤ 5 candidates per run | BERECHNET (settings) | NICHT VERIFIZIERT |
| GeckoTerminal | ≈ 288 a day, ≈ 345 with page 2 (cap 72) | BERECHNET | public API, free tier: NICHT VERIFIZIERT |
| Holder provider (Blockscout) | one page per ATLAS read (`atlas_holder_max_pages` 1) | BERECHNET | public explorer: NICHT VERIFIZIERT |
| ORBIT model | ≤ 10 reviews per run, ≤ 96 a day; ≤ 1 024 output tokens each | BERECHNET (caps) | Anthropic: 96 × (input + output tokens) × price, NICHT VERIFIZIERT; Codex CLI: inside the ChatGPT plan, NICHT VERIFIZIERT |
| Trading host | 2 vCPU / 8 GiB / 40 GiB SSD | GESCHÄTZT | about 15–40 EUR a month at common VPS providers, NICHT VERIFIZIERT |
| Dead-man receiver | 1 vCPU / 1 GiB | GESCHÄTZT | about 4–6 EUR a month, NICHT VERIFIZIERT |
| Backups | runtime database 59 MiB (GEMESSEN); 14 nightly dumps < 2 GiB | GESCHÄTZT | object storage under 1 EUR a month, NICHT VERIFIZIERT |
| Alerting | Telegram bot API | — | free, NICHT VERIFIZIERT |

## 6. Offline acceptance

| Case | Evidence |
|---|---|
| Clean system start | `tests/ops/test_jobs_linux.py` (install starts nothing; boot order exit → index → scout → entry; entry refused until exits are watched); `tests/ops/test_exit_supervisor_linux.py`; `supervisor/test_linux.py::test_a_changed_boot_identity_is_a_host_restart` |
| Database unreachable | `runner/test_exit_job_faults.py` (`DATABASE_UNAVAILABLE`, nothing sold, not ready); `supervisor/test_models.py` (CRITICAL); `runner/test_preflight.py`; entry wrapper refuses on a not-ready preflight |
| Index incomplete | `lpcontrol/test_pool_discovery.py`, `atlas/v4/test_pool_index.py` (census refuses an index behind the chain); partial index run exit 3 passed on (`test_jobs_linux.py`) |
| Exit job fails | `supervisor/test_tick.py` (crash, hang, ignored SIGTERM, process group killed); `jobruns/test_liveness.py` (stale or failed evidence refuses every BUY) |
| Entry job held | `jobruns/test_split_jobs.py::test_the_entry_job_on_stale_or_failed_evidence_starts_nothing`; `test_jobs_linux.py` (no supervision, failed timer, unknown systemd, preflight 1/2) |
| Dead-man stays silent | `supervisor/test_deadman.py` (missing or old heartbeat CRITICAL, rejected heartbeats never refresh liveness, receiver state survives restart) |
| Network interruption | `paperexit/test_exit_rpc_failures.py`, `paperexit/test_exit_acceptance.py` (timeouts refused by name, retried); `supervisor/test_alerts.py::test_a_failed_send_is_retried_on_the_next_pass` |
| Process restart | `runner/test_exits_once.py` (cancelled run and dead connection release their lock); `supervisor/test_tick.py::test_a_restart_continues_from_the_recorded_state` (`PREVIOUS_TICK_ABANDONED`) |
| Power loss (simulated) | boot-identity change → `HOST_RESTART_DETECTED`; abandoned tick detected; every sale idempotent by key and settled by reading it (`exitpolicy/test_isolation.py`, `test_drive.py`) |
| No duplicate orders | `paperexit/test_concurrency.py`, `runner/test_exit_phase.py` and `test_exits_once.py` (PostgreSQL races), request-key replay tests |
| No lost exit orders | `exitpolicy/test_isolation.py` (failed position retried next run), coverage gate `EXIT_POLICY_COVERAGE_MISSING` (`jobruns/test_liveness.py`) |
| Restore from backup | the rehearsal in section 4 (real, scratch databases); `tests/ops/test_db_rehearsal.py` |

## 7. Still open: the 72-hour host acceptance

Only on the real host, after approval: 24 hours of exit runs all
`exit_ready=true`, one deliberately failed run alerted within its interval,
one heartbeat outage alerted by the receiver, one planned reboot with every
timer back and `HOST_RESTART_DETECTED` reported, a restore probe from the
host's own backup — then the 72-hour window with the exit job, the database
and the network continuously available. Until then: **NO-GO**.
