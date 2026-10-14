# Exit supervisor on Linux: systemd, alerting, dead-man's switch, and the move off the Mac

This is the Linux counterpart of [the exit supervisor](exit-supervisor.md). It covers:

- the systemd units and operator scripts;
- the Telegram alert pass;
- the dead-man's switch;
- the deploy gate on both hosts;
- a read-only assessment of the existing shared VPS;
- the plan for moving the runtime from the Mac to Linux.

**Nothing here is installed or activated.** All units and scripts exist only as repository files.

## Status

| Item | State |
|---|---|
| Implemented | systemd service and timer for the tick (`ops/exit-supervisor/linux`); operator scripts (install, start, stop, stop-alerts, uninstall, health, deploy-check); the alert pass (`python -m src.supervisor.main alert`, Telegram transport); the heartbeat sender (`... main heartbeat`) and the dead-man receiver (`python -m src.supervisor.deadman receive\|check`); host-restart detection; the deploy gate now also refuses a running entry job or a live runner process (macOS and Linux). |
| Tested | Pure decision tables for alerts and heartbeats. Telegram through `httpx.MockTransport`. Scripts run against stand-ins for `systemctl`, the backend Python, `pgrep` and `ssh`. Unit files are parsed and checked. Nothing talks to Telegram, systemd or a VPS. |
| Not operationally ready | No host approved (see [the VPS assessment](#the-existing-vps-read-only-assessment)). No alert credentials provisioned. No dead-man receiver. The runtime is not migrated. |
| Activation approved | **No.** No unit installed, no timer enabled, no message sent, `DEAD_MAN_ACTIVE = NO`. |

## Units

| Unit | Kind | Runs | Credentials |
|---|---|---|---|
| `rh-agents-exit-supervisor.service` | oneshot | `run-exit-supervisor.sh` → `python -m src.supervisor.main tick` → `--exits-once` | none |
| `rh-agents-exit-supervisor.timer` | timer | the service above, every 60 s, from boot on | — |
| `rh-agents-exit-alert.service` | oneshot | `run-alert.sh` → `python -m src.supervisor.main alert` | `telegram-token`, `telegram-chat-id` |
| `rh-agents-exit-alert.timer` | timer | the alert pass, every 60 s, from boot on | — |
| `rh-agents-heartbeat.service` | oneshot | `run-heartbeat.sh` → `main heartbeat` piped over pinned SSH | `heartbeat-key`, `heartbeat-ssh-key`, `heartbeat-known-hosts` |
| `rh-agents-heartbeat.timer` | timer | the heartbeat, every 60 s, from boot on | — |

### Same guarantees as on macOS

| Guarantee | How it holds on Linux |
|---|---|
| Exit job only | The tick is the unchanged `src.supervisor.tick`. It builds only the constant `EXIT_COMMAND` (`--exits-once`). Tests check that no script contains another runner mode or `src.runner.main` at all. |
| 60 s interval, 60 s exit budget | Timer `OnUnitActiveSec=60`. A `PAPER_EXIT_RUN_MAX_SECONDS` above 60 is refused before anything starts, as before. |
| No overlap | systemd never starts a oneshot unit that is still active. The tick's `flock` and the exit job's `PAPER_EXIT_JOB` advisory lock still apply. |
| Bounded hard timeout | The tick kills its child's process group after 85 s, plus a 5 s grace. In addition, `TimeoutStartSec=120` together with `KillMode=control-group` makes systemd kill everything in the unit's cgroup. This also catches descendants that left the process group. |
| Persistent evidence, 180 s liveness | Unchanged. The child writes `paper_job_runs`, and `judge_exit_liveness` reads it. |
| Restart after reboot | The timers are `WantedBy=timers.target`, so once enabled they start at every boot. A boot changes `/proc/sys/kernel/random/boot_id`. The next tick records `HOST_RESTART_DETECTED`, and health reports `HOST_RESTARTED_RECENTLY` (DEGRADED) for 15 minutes, long enough for the alert pass to see it. |
| Install does not activate | `install.sh` writes the units, runs `daemon-reload` and `disable` on every timer. It never runs `enable` or `start`. Only `start.sh` enables units. |
| Explicit start | `start.sh --confirm --reason CODE`, audited. It is refused without alert credentials: exit supervision never starts without a way to raise an alarm. The alert timer starts before the supervisor's, and one alert pass must then **succeed synchronously**: credentials readable and valid, the message delivered. While the supervisor is still stopped, that pass sends a real `STOPPED` alert, which proves delivery. If it fails, the alert timer is disabled again and the supervisor is not started. `--with-heartbeat` proves the heartbeat the same way. At boot the alert timer fires first (30 s, against 60 s for the tick). |
| Guarded stop | Same rules as on macOS (`stop-check`). It is also refused while the tick unit is anything but inactive or failed; an unknown state also refuses. Afterwards the script checks that the timer actually stopped. |
| Runtime checkout only | The wrapper refuses (exit 78) a checkout that is not on `main`, or that has modified **or untracked** files (`git status --porcelain`; ignored files such as `.env` and `.venv` are not listed). Scripts run as root pass `safe.directory` for this one checkout, so git's ownership check does not block them. |
| Emergency stop | Needs `--emergency --i-understand-exit-monitoring-stops`. It prints a warning and writes an audit entry; if the audit fails, the stop still proceeds. It stops the running tick too, never liquidates, and is verified. |
| Alerts outlive the supervisor | `stop.sh` stops only the supervisor. `stop-alerts.sh` is refused while the supervisor timer is not stopped. |
| Limited rights and resources | The units run as the service user, never root. They set `NoNewPrivileges`, an empty capability set, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`, kernel and cgroup protection, `RestrictNamespaces`, `RestrictSUIDSGID` and `UMask=0077`. The tick is capped at `MemoryHigh=600M`/`MemoryMax=800M`, `CPUQuota=100%`, `TasksMax=64`, Nice 5, IOWeight 50. Alert and heartbeat are capped at 400 MB and 50 % CPU. |
| Secrets | Database settings live in the checkout's `.env` (0600, service user). Telegram and heartbeat secrets reach only their own unit, as systemd credentials (`LoadCredential=`). The tick that runs the exit job never sees them. The journal receives codes only. |

### Operating

```bash
# as root, from the runtime checkout on main (/srv/rh-agents/app)
ops/exit-supervisor/linux/install.sh --host-alias rh-vps [--heartbeat-target deadman]
ops/exit-supervisor/linux/start.sh --confirm --reason FIRST_LINUX_RUN [--with-heartbeat]
ops/exit-supervisor/linux/health.sh          # 0 HEALTHY, 1 DEGRADED, 2 CRITICAL, 3 STOPPED
ops/exit-supervisor/linux/stop.sh --reason DONE
ops/exit-supervisor/linux/stop-alerts.sh --reason MAINTENANCE
ops/exit-supervisor/linux/uninstall.sh
journalctl -u rh-agents-exit-supervisor.service -u rh-agents-exit-alert.service
```

Scripts run as root drop to the service user (`runuser`) for every supervisor command. Status comes from `systemctl show` (`RH_AGENTS_SUPERVISOR_BACKEND=systemd`):

| Timer and tick state | Meaning |
|---|---|
| Timer `active` | supervising |
| Timer `inactive` or not found | not loaded |
| Timer in any other state, or `systemctl` unanswerable | unknown, never HEALTHY |
| Tick unit `activating` | a tick is running |

## Alerting

`python -m src.supervisor.main alert` runs once a minute from its own timer. It computes the same read-only health verdict as `health.sh`, compares it with the persisted alert state (`/var/lib/rh-agents/alerts/alert-state.json`, 0600, atomic, `flock`) and sends at most one message.

| Situation | Message |
|---|---|
| First verdict ever, HEALTHY | none (silent baseline) |
| First verdict ever, not HEALTHY | ALERT |
| Status change (for example HEALTHY→DEGRADED, HEALTHY→CRITICAL, →STOPPED) | ALERT; ESCALATION when severity rises from a non-HEALTHY status (for example DEGRADED→CRITICAL) |
| New kind of reason under an unchanged DEGRADED or CRITICAL | CHANGE (`CONSECUTIVE_FAILURES:3`→`:4` is not new) |
| Unchanged DEGRADED or CRITICAL | REPEAT every 30 minutes |
| Unchanged STOPPED | none |
| HEALTHY again after a delivered non-HEALTHY | RECOVERY, only after **two** HEALTHY passes in a row; a single healthy pass is no recovery |
| More than 6 messages in an hour | held back and counted (`suppressed`), and the count is named in the next message; news is sent once the hour allows. A move **into CRITICAL** (ALERT or ESCALATION) is never held back. Delivery times in the future (a clock set back) are ignored rather than holding the cap shut. |
| Alert state cannot be read (I/O error) | nothing sent, `ALERT_STATE_UNREADABLE` (exit 1). Only a corrupt file is reset and reported as `state_reset`. |
| Delivery failed | nothing marked delivered; retried on the next pass |
| Health check crashed, or status unknown | CRITICAL `HEALTH_CHECK_FAILED` / `HEALTH_STATUS_UNKNOWN` |

These cover the conditions the order asks for:

| Condition | Reason code |
|---|---|
| Stale exit liveness | `OPEN_POSITIONS_EXIT_STALE:*` (CRITICAL with positions) or `EXIT_STALE_ENTRIES_BLOCKED:*` (DEGRADED without) |
| Failed exit job | `CONSECUTIVE_FAILURES:n` and `SERVICE_LAST_EXIT_NONZERO` |
| Database unreachable | `DATABASE_UNREADABLE_POSITIONS_UNKNOWN` (CRITICAL) |
| Provider or RPC failure | The exit run's `exit_ready_reason`, as category and code in the state file, which ends in `CONSECUTIVE_FAILURES` |
| Open position without supervision | `OPEN_POSITIONS_SUPERVISOR_NOT_RUNNING` |
| Supervisor down | `SUPERVISOR_HEARTBEAT_STALE`, or the status changes to STOPPED or CRITICAL |
| Recovery | RECOVERY, after two HEALTHY passes in a row |

**Message contents are fixed:**

```
RH-AGENTS ALERT: CRITICAL
host: rh-vps
reasons: OPEN_POSITIONS_SUPERVISOR_NOT_RUNNING
open positions: 1
at: 2026-10-14T08:00:00Z
```

Reasons must match `^[A-Z][A-Z0-9_]{0,63}(:[A-Z0-9_]{1,32})?$`; anything else is dropped. The host alias must be a short lowercase name, so hostnames and addresses become `unnamed-host`. No free text, URL, connection string or token can reach a message. Tests feed it a connection string, a token and a traceback.

**Transport:** Telegram `sendMessage` (`RH_AGENTS_ALERT_TRANSPORT=telegram`).

- The token and chat id are read from owner-only files, by default from the systemd credentials `telegram-token` and `telegram-chat-id`. Both are validated. systemd grants a credential to the service user through an ACL, which shows up as group bits. Under `$CREDENTIALS_DIRECTORY` the rule is therefore: owner root or the service user, and no bits for others. Everywhere else, no group or other bits at all.
- `httpx` request logging is held at WARNING, because its INFO lines would contain the token URL.
- Every failure becomes a code (`SEND_TIMEOUT`, `SEND_HTTP_502`, `SEND_RATE_LIMITED`, …). No exception text is kept, because the request URL contains the token.
- `--dry-run` prints the text that would be sent and records nothing.

**Choosing a bot.** The existing `@rhplatform_bot` (private chat, send-only) is a candidate channel. Choosing it and putting its token into the credentials directory is an operator step; this repository never reads or copies an existing token. Running without a transport is refused (exit 2), and `start.sh` refuses without credentials.

## Dead-man's switch

An alert pass on the trading host dies with the host, so the host also sends heartbeats, and a receiver **on another machine** raises the alarm when they stop.

| Part | Where | What |
|---|---|---|
| `python -m src.supervisor.main heartbeat` | trading host, `rh-agents-heartbeat.timer`, every 60 s | Prints one signed line: `{payload: {v, host, sent_at (ms, UTC), nonce, status}, mac}`. The MAC is HMAC-SHA256 over the canonical payload, with a shared key of at least 32 bytes. |
| `run-heartbeat.sh` | trading host | Pipes the line to `ssh -T -F /dev/null -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=<credential> -o GlobalKnownHostsFile=/dev/null -p <port>` with a dedicated key, to `[user@]host` (`install.sh --heartbeat-target`, `--heartbeat-port`). The receiver's host key is pinned. |
| `python -m src.supervisor.deadman receive` | receiver, as the forced command of that one key | Verifies MAC (64 hex digits, constant-time comparison), host, version and nonce. Accepts `sent_at` only within ±120 s of the receiver's clock **and** later than the last accepted one: no replay, no reordering, also across restarts. Rejections are counted and refresh nothing. |
| `python -m src.supervisor.deadman check` | receiver, its own timer every 60 s | No heartbeat yet: CRITICAL `HOST_HEARTBEAT_MISSING`. Older than 300 s: CRITICAL `HOST_HEARTBEAT_STALE`. A fresh heartbeat reporting CRITICAL: CRITICAL `HOST_REPORTS_CRITICAL` (the alarm gets out even when the host's own alert path broke). A fresh heartbeat without a readable status, or a receipt time in the future: CRITICAL. Otherwise HEALTHY. Uses the same alert engine (deduplication, repeat, cap, recovery) with its own state. |

**Receiver properties:**

- It imports neither settings nor database code; a test verifies this.
- It holds no trading secret, reads no trading database and writes only its own state directory.
- It stores one record per host, so storage is bounded.
- Its authorized-keys line looks like this:
  `restrict,command="/srv/rh-deadman/app/backend/.venv/bin/python -m src.supervisor.deadman receive --store /var/lib/rh-deadman --host rh-vps" ssh-ed25519 …`
- It needs its own Telegram credentials, provisioned by the operator.

**Possible counterparts, checked read-only:**

| Option | Assessment |
|---|---|
| The Mac | It sleeps, has FileVault without auto-login, and is the host being replaced. Acceptable only as a stopgap while someone is present. |
| A second machine already authorized for RH Agents | None exists. |
| An external heartbeat service | It would be a new external service, so it needs an explicit approval and an account. The receiver's interface (one signed line) maps directly onto an HTTPS check-in if one is approved. |

**`DEAD_MAN_ACTIVE = NO`.** The components and interfaces are ready; there is no authorized counterpart yet.

## Deploy gate

A deploy changes the code the next tick runs, and the schema may lag that code until the migration is applied. Run `deploy-check.sh` before **every** step that changes what the runtime runs: git fast-forward, `uv sync`, `alembic upgrade`, a unit reinstall, a supervisor restart. Abort on any non-zero exit.

| Host | Command |
|---|---|
| macOS (today) | `ops/exit-supervisor/deploy-check.sh` |
| Linux (planned) | `ops/exit-supervisor/linux/deploy-check.sh` |

Both refuse (`DEPLOY_REFUSED`, exit 2) when any of these holds:

| Condition | Refusal |
|---|---|
| Supervisor loaded, or its state unknown | `DEPLOY_REFUSED_SUPERVISOR_LOADED`, `…_SERVICE_STATUS_UNKNOWN` |
| Position open, or positions unknown | `DEPLOY_REFUSED_OPEN_POSITIONS`, `…_POSITIONS_UNKNOWN` |
| Exit or entry run recorded as going | `DEPLOY_REFUSED_EXIT_RUN_ACTIVE`, `…_ENTRY_RUN_ACTIVE` |
| Runner or tick process alive, or the process list unreadable | `DEPLOY_REFUSED_RUNNER_PROCESS_ALIVE`, `…_PROCESS_LIST_UNKNOWN` |
| The check itself crashes | non-zero exit, refused |

The minimal runbook on either host:

1. Empty book.
2. Entries halted (`python -m src.runner.control --halt-entries --reason DEPLOY`).
3. `stop.sh --reason DEPLOY`.
4. `deploy-check.sh` must exit 0.
5. On macOS only: the scout unload gate.
6. `git pull --ff-only`, then `uv sync --locked`, then `alembic upgrade head`.
7. Verify.
8. `start.sh --confirm --reason DEPLOY_DONE`.
9. `health.sh`.

The first deploy that brings this code to a host still runs the old code, which has no gate: do the same checks by hand, once. Nothing here changes any deploy pipeline; the RH Agents runtime deploy is a manual runbook today.

### Known limits

- `systemctl show` is asked as an unprivileged user over D-Bus. Without D-Bus, the status is always unknown. That fails closed, but it must be checked on the host.
- Only the timer's `ActiveState` is judged. A timer started by hand but never enabled reads as supervising and would not survive a reboot. Use only `start.sh`, which enables.
- The units were checked by tests and by reading them, not by `systemd-analyze verify` on a real host. Do that before the first install.

## The existing VPS: read-only assessment

Assessed on 2026-10-14 through an existing, authorized SSH alias, read-only. No sudo was used and no configuration, secret or foreign file was opened. Hostnames and addresses are deliberately omitted.

| Item | Measured |
|---|---|
| OS / kernel / arch | Debian GNU/Linux 13 (trixie), Linux 6.12 (cloud), x86_64 |
| CPU | 4 vCPU (QEMU virtual CPU) |
| RAM | 7.9 GiB total, about 2.4 GiB available |
| Swap | 4 GiB, **2.6 GiB in use**; committed memory about 150 % of the commit limit |
| Memory pressure | PSI at the time about 0; little stall time accumulated since boot |
| Disk | 197 GB root, 121 GB free (37 % used) |
| Load | 0.94 / 0.54 / 0.39 (1/5/15 min) |
| Uptime / reboots | 31 days. A reboot is pending (`/var/run/reboot-required`). Unattended upgrades are enabled. |
| systemd | 257, system state `running` |
| PostgreSQL | **not installed natively** (no `psql`, no cluster); Debian 13 ships PostgreSQL 17 |
| Containers | Docker, with four containers of other projects (one of them a database), all healthy |
| Other services | nginx, fail2ban, a certificate renewal timer, user services of other projects; the largest single process holds about 2.9 GB RSS |
| Backup | none visible beyond the package database backup timer |
| Monitoring | none visible (no exporter or monitoring agent) |
| Access available to us | An unprivileged account shared with other projects. It is in the `sudo` group, but sudo needs a password we do not have and must not use. That account is also in the `docker` group, which is root-equivalent. |

**RH Agents' needs, estimated, not measured on Linux:**

| Component | Basis | Estimate |
|---|---|---|
| Runner/tick Python process | measured: about 121 MB RSS after importing the runner (macOS arm64) | 120–200 MB per process; the tick is parent plus child, for up to 60 s a minute |
| Alert pass | same interpreter, no child | 120–150 MB for seconds a minute |
| PostgreSQL 17, small config | measured: runtime DB is 59 MB; config `shared_buffers=128MB`, `max_connections=20` | 150–250 MB |
| Exit-only steady state | sum of the above | about 0.5–0.7 GB peak |
| Scout, ORBIT/Codex (Node), funding graph | not measured | another 0.4–0.8 GB at peaks (unverified) |
| Network | provider/RPC requests per exit run with positions; scout requests | small (kB–MB per minute; unverified) |
| Logs / backups | journald with codes only; `pg_dump` of a 59 MB DB | negligible / about 60 MB per dump |

**Verdict: `VPS_NO_GO` as it stands, conditional GO for exit-only after these preconditions.**

1. **Isolation needs root, and we have none.** A dedicated user, native PostgreSQL, system units and the credentials directory all require root. The only access available is an account shared with other projects, which is also in the root-equivalent `docker` group. Running RH Agents under that account would break isolation, so it is ruled out.
2. **Memory headroom is thin.** About 2.4 GiB is available while 2.6 GiB of swap is already in use and memory is overcommitted. Exit-only RH Agents (about 0.5–0.7 GB, capped by `MemoryMax`) would fit. The full early-only stack with Codex would push the host further into swap and put the other projects' production services at risk. Before more than exit-only runs there, add RAM (16 GB) or confirm measured headroom.
3. **A reboot is pending and unattended upgrades are on.** Reboots must be coordinated with the other projects' owners. The supervisor survives a reboot by design: timers restart, and the restart is reported.
4. **No backup and no monitoring exist on the host**, so RH Agents must bring its own: `pg_dump` with off-host copies, and the dead-man receiver.

**Recommendation:** a dedicated small Linux VPS (Debian 13, 2–4 vCPU, 4–8 GB RAM, native PostgreSQL 17, systemd) remains the cleanest host. It matches ARCHITECTURE.md and shares no blast radius with production services of other projects. Ordering one is the owner's decision. If the shared VPS is chosen anyway, apply the isolation below, run exit-only first, and treat preconditions 1–4 as gates.

## Isolation architecture (for either Linux host)

**Native systemd, not containers.** AGENTS.md mandates native host processes. Docker group membership is root-equivalent and must never be given to the RH Agents user. systemd already provides cgroup limits, privilege dropping and credentials without another daemon.

| Concern | Design |
|---|---|
| Identity | System user and group `rh-agents` (no login shell, home `/var/lib/rh-agents`), no sudo, no docker group. |
| Code | `/srv/rh-agents/app`, a clone of this repository on `main`, owned by `rh-agents`, mode 0750; virtualenv at `backend/.venv` via `uv sync --locked`. |
| Configuration | `/srv/rh-agents/app/.env`, mode 0600, owner `rh-agents`. `DATABASE_URL` uses the native Unix socket, with no password: `postgresql+asyncpg://rh_agents@/rh_agents?host=/var/run/postgresql` (the form README documents). |
| Database | Native PostgreSQL 17 from Debian, listening only on the local socket (`listen_addresses = ''`). Role `rh_agents` with `NOSUPERUSER NOCREATEDB NOCREATEROLE` owns database `rh_agents`. `pg_hba`: `local rh_agents rh_agents peer map=rh`, with `pg_ident` mapping `rh-agents` to `rh_agents`. No shared superuser, no other project's database. Small config (`shared_buffers=128MB`, `max_connections=20`) plus a systemd drop-in `MemoryMax=400M` on the cluster unit. |
| Secrets | Telegram and heartbeat credentials live in `/etc/rh-agents/credentials/`, owner root, mode 0600, and reach only their unit through `LoadCredential=`. There is no shared trading credential store; Phase 0 has no signing keys at all. |
| State and logs | `/var/lib/rh-agents/{exit-supervisor,alerts}` (`StateDirectory=`, 0700); journald for output (codes only); the audit log in the state directory. |
| Processes | Every unit runs as `rh-agents` with the hardening and limits listed above, so no access to other projects' files (`ProtectHome`, read-only system). |
| Network | Outbound only: providers, RPC, Telegram, SSH to the dead-man receiver. No listening port; the web UI is not part of this setup. |

## Migration plan: Mac → Linux (not executed)

Starting point (2026-10-14):

| Item | State |
|---|---|
| Runtime code on the Mac | `cf256649a6c1df646a8fb9d133a60c24b84365d3` |
| `main` | `ee503fff9868b722aba500e4dc05dafbc2893204` |
| Database | PostgreSQL 17.11, database `rh_agents`, Alembic 0024 |
| Positions and runners | 0 positions; PAPER runner disabled |
| Exit jobs | 3 successful exit-only jobs |
| Other | Early-scout diagnostics done, Alchemy key rotated |

Each step needs its own approval where it touches a host.

| # | Step | Gate |
|---|---|---|
| 1 | **Host setup** (root, by the owner): updates applied, pending reboot done, time sync on, firewall unchanged (outbound only). | Host decision made (dedicated or shared). |
| 2 | **User**: `adduser --system --group --home /var/lib/rh-agents --shell /usr/sbin/nologin rh-agents`. | Not in the `sudo` or `docker` group. |
| 3 | **Database**: `apt install postgresql-17`; socket-only; role `rh_agents` without superuser rights; database `rh_agents`; peer map; memory drop-in. | `psql -c 'select version()'` as `rh-agents`: 17.x. |
| 4 | **Code**: clone to `/srv/rh-agents/app`, check out `main` at the approved SHA; `uv sync --locked --compile-bytecode` as `rh-agents`. The checkout is read-only to the units, so bytecode must exist beforehand. If `.env` names `REASONING_PROVIDER=codex`, install the Codex CLI where the units' PATH finds it; otherwise health keeps reporting `CODEX_NOT_ON_PATH`. | `git status` clean; SHA equals the approved SHA. |
| 5 | **Secrets without logging**: copy `.env` with `scp` or an `install -m 0600` pipe straight into place, never through a terminal echo or shell history. Turn `DATABASE_URL` into the local socket URL. Telegram and heartbeat credentials go to `/etc/rh-agents/credentials` as 0600 root. | `stat` shows 0600 and the correct owners; values are never printed (allowlist checks only). |
| 6 | **Backup and restore**: on the Mac, after a regular stop with an empty book, `pg_dump -Fc rh_agents` with its SHA-256 recorded. Transfer it, then `pg_restore --no-owner --role=rh_agents -d rh_agents`. Keep the Mac DB untouched as the fallback. | The dump checksum matches on both sides. |
| 7 | **Integrity**: compare row counts of every table and `max(created_at)` of the key tables on both sides (read-only queries). | All counts equal. |
| 8 | **Schema**: `alembic current` = 0024 = head of the deployed code; no migration runs during the move. | `alembic check` clean. |
| 9 | **Supervisor install**: `deploy-check.sh` (exit 0), then `install.sh --host-alias <alias>` (disabled). | `systemctl is-enabled` reports disabled for all three timers. |
| 10 | **Health and alerting acceptance**: `alert --dry-run`; then one deliberate test message after approval; `health.sh` STOPPED while idle. | The message arrives and contains only codes. |
| 11 | **External heartbeat**: provision a receiver after approval, install the forced-command key, pin the host key; `start.sh --with-heartbeat` later. Test by stopping the heartbeat timer: CRITICAL `HOST_HEARTBEAT_STALE` within 5 minutes. | The dead-man alarm is observed once. |
| 12 | **Provider and RPC readiness**: run the existing read-only provider and RPC checks from the VPS (rate limits, latency, the Alchemy key restricted to the new egress if the provider supports it). | No errors. |
| 13 | **Exit-only smoke**: `start.sh --confirm --reason LINUX_EXIT_SMOKE`; watch at least 10 ticks; `health.sh` HEALTHY; test a reboot during the window: timers come back and `HOST_RESTARTED_RECENTLY` is reported, then clears. | 10/10 exit runs `exit_ready`; no overlap. |
| 14 | **Early-only PAPER entry test**: Mac runtime confirmed stopped (never two runtimes on one book). Then a single `--entries-once` under the live supervisor, following the step F runbook. | Entry, exit supervision and alerts are observed end to end. |
| 15 | **Regular 24/7 operation**: scheduler on, weekly `pg_dump` off-host, monthly restore test, alert and dead-man drills. | Owner approval. |

The Mac runtime stays as the cold fallback until step 15. It is never run at the same time against a migrated book.
