# PAPER_MINIMUM_RELEASE_GATE

The closed, checkable list of what must hold before the **first** PAPER
operation of RobinHood Agents (Issues #77, #40). It is binding for that
decision only. It does not authorise anything by itself: every step that
touches the runtime — migration, deploy, scheduler, activation — needs Kai's
explicit approval (P0-O).

## Scope rule

- **P0** — must hold before the first PAPER operation.
- **P1** — needed for reliable continuous operation; moved to P0 only with a
  demonstrated risk that prevents a safe first operation.
- **P2** — optimisation, comfort, later extension.

A new finding is **not** P0 by default. A P0 claim must state (1) the concrete
safety or data-integrity fault, (2) the executable path it occurs on, (3) why
the existing protections do not catch it, (4) why a bounded PAPER test with the
risky functions disabled cannot avoid it, and (5) the smallest fix. Without that
it is filed as an issue (P1/P2) and does not block.

The safety contract is not relaxed by this list: fail-closed, idempotency,
account locking, SELL safety checks and database integrity stay as they are.

## P0 — before the first PAPER operation

Status: **CODE** = implemented and tested in the repository; **OPS** = to be
done and verified in the ops window (runbook below); **DECISION** = Kai.

| # | Requirement | Status | How it is verified |
|---|---|---|---|
| A | Entry and exit jobs are separate (`--entries-once` buys and never sells; `--exits-once` sells and never buys; `--once` unchanged) | CODE (#77 job separation PR) | `tests/jobruns/test_split_jobs.py`, `tests/runner/test_exits_once.py` |
| B | `EXIT_JOB_STALE` is enforced under the final BUY guard (risk request, fill, execution boundary) | CODE | `tests/jobruns/test_liveness.py` incl. old approval, boundary and PostgreSQL lock-queue tests |
| C | Migrations 0023 and 0024 apply to the runtime schema, in that order | OPS | runbook steps 5–6 (`alembic upgrade 0023`, check, `alembic upgrade 0024`, check, `alembic check` on a restored copy first) |
| D | New code is deployed only after both migrations succeeded | OPS | runbook step 8; old code on the new schema keeps working (additive), new code on the old schema fails closed |
| E | No existing system pause is reset silently | CODE + OPS | migrations never touch `paused`; lifting is only `--clear-system-pause` with its audit, refused for every origin but the pre-#85 daily loss pause (`tests/entryhalt/test_pause_recovery.py`, `tests/jobruns/test_migration.py`) |
| F | The exposed Alchemy key is rotated/revoked; new credentials configured safely | OPS (Kai) | provider console shows the old key revoked; runtime `.env` holds the new one, never printed |
| G | PAPER mode is binding; LIVE execution is impossible | CODE | Settings refuse a run unless `TRADING_MODE=PAPER`; no signing library or live path exists (Phase 0); preflight |
| H | Entry limits, daily loss limit, kill switch, exit policies, idempotency checked | CODE | existing suites (risk, casefill, paperexit, entryhalt, exitpolicy); runbook step 7 checks the configured values |
| I | Market acquisition for held positions and the pre-exit refresh are on and working | OPS + CODE | `PAPER_RUNNER_MARKET_ACQUISITION_ENABLED=true`; an exit run is only evidence when every held market was answered (`EXIT_ACQUISITION_INCOMPLETE` otherwise) |
| J | A usable exit path exists; no open position is held without an exit policy | CODE + OPS | exit evidence requires full coverage (`EXIT_POLICY_COVERAGE_MISSING`), no blocked/failed/unknown exit; runbook step 9 reads coverage of the existing book |
| K | A successful exit job is recognised as live | CODE | `test_a_working_exit_run_is_recorded_as_evidence_for_its_own_classes`, `test_an_exit_run_with_nothing_open_is_evidence` |
| L | A failed exit job can never count as healthy | CODE | timeout, crash/unfinished, acquisition gap, blocked exit, failed attempt, unknown outcome, missing coverage, `ALREADY_RUNNING` — all not evidence (`tests/jobruns`) |
| M | Operation is observable through safe status/error codes | CODE + OPS | `paper_job_runs` (outcome, stop, `exit_ready_reason`, codes, counts), run summaries, `control --status/--events/--pause-origin`; runbook step 9 defines the checks |
| N | No unresolved P0 finding from security review and regressions | DECISION | review of the job-separation PR; full SQLite + PostgreSQL suites and CI green |
| O | Kai's explicit approval before migration, deploy, scheduler start and PAPER activation — each separately | DECISION | recorded in the issue before each step |

**Exit budgets and provider limits.** Before *continuous* operation a short,
bounded smoke test under real provider conditions must show that exit runs
repeatedly finish within their bounds with `exit_ready=true`. Positions that
stay blocked or unobserved are not an acceptable P1 deferral: the gate already
turns them into `EXIT_JOB_STALE` (no new BUY), and the smoke test must show that
this is not the steady state.

## P1 — after the first operation, unless a demonstrated blocker

- Persistent fairness queue with full anti-starvation history for exits.
- Long-term monitoring and alerting on `paper_job_runs` (stale exit job, repeated
  `exit_ready=false`, `JOB_RUN_UNRECORDED`).
- Richer Telegram reports.
- Additional performance metrics.
- Extended operator interface; comfortable recovery workflows.
- Provider caching and rate-limit optimisation.
- Operator authentication and separate database roles (today: whoever can run
  the control module with the runtime `DATABASE_URL` is the operator; the audit
  makes changes *recorded*, not *authorised*).
- One loss notion for the automatic halt (a SELL halts on realised loss; a BUY
  is judged on realised + unrealised loss) — documented, not a gap in protection.
- Finer classification of exit refusals for health (today every refused
  triggered exit counts as `EXIT_BLOCKED`, conservatively).
- Retention for `paper_job_runs`.

## P2 — later

- Extended dashboards; comfort and UX functions.
- Additional reporting channels.
- Further automation of operator processes.
- Further strategy optimisation.

## Lifting an existing system pause

`paper_accounts.paused` is the full stop. Two things have ever set it, both
together with a SENTINEL `PAUSE_SYSTEM` verdict that is recorded: the kill
switch (or an already paused account), and — before #85 — the daily loss
limit. The pause itself stores no reason, so its origin is read from that
record (`control --pause-origin`).

**Implemented, audited, narrow:** `control --clear-system-pause` lifts a pause
only if all of these hold, read under the paper account lock:

1. the account is paused;
2. the kill switch is off in the configuration the tool runs with;
3. the first `PAUSE_SYSTEM` verdict after the last audited lift names
   `DAILY_LOSS_LIMIT` and not `KILL_SWITCH` (the pre-#85 daily loss pause);
4. every verdict after it is a `PAUSE_SYSTEM` naming `KILL_SWITCH` — what a
   verdict reached while paused always is — so the record shows one
   uninterrupted pause;
5. the operator names exactly that verdict (`--confirm-pause-origin`);
6. an entry halt (manual or automatic) is already in force, so lifting the pause
   reopens sales only.

Every success and every refusal is a `paper_control_events` row
(`control=SYSTEM_PAUSE`). PostgreSQL refuses any commit that turns `paused` off
without such a `CLEAR` event in the same transaction, and only
`actor_kind=OPERATOR` may write one. Nothing lifts a pause on its own, a new day
does not, and no web route or agent can.

**Residual risk.** A pause set or lifted by hand before 0024 leaves no trace.
It reads as `UNKNOWN` or `INCONSISTENT` and is refused — except one case: a
genuine pre-#85 daily loss pause that was later lifted by hand and set again by
hand with no verdict in between would read as the original. Step 3 of the
runbook therefore requires the operator to confirm from the database logs /
operator history that `paused` was never edited by hand.

**Every other origin (`KILL_SWITCH`, `UNKNOWN`, `INCONSISTENT`) is not lifted by
any tool.** Its recovery is an operator procedure that needs its own approval:

1. Stop every job; keep the pause.
2. Establish the cause from evidence: the origin verdict (`--pause-origin`), the
   runs around it (`paper_job_runs`, logs), and for a kill-switch pause, whether
   `COMMANDER_KILL_SWITCH` is still set and why it was set.
3. Resolve the cause (kill switch off only with its own approval).
4. Set a manual entry halt (`control --halt-entries --reason PAUSE_RECOVERY`).
5. Write a reviewed, one-off change that lifts `paused` **with** a
   `SYSTEM_PAUSE` `CLEAR` event in the same transaction (the database enforces
   the event), naming the reviewed origin in `risk_decision_id` where one
   exists; record the approval in the issue.
6. Start the exit job only, confirm `exit_ready=true`, then decide separately
   about lifting the manual entry halt.

## Draft runbook: the ops window (READ-ONLY — not executed)

This runbook is a draft. Nothing in it has been run against the runtime. Each
step that changes something needs Kai's explicit approval at that moment.

**Go/No-Go: any `NO` stops the window, and the runtime stays as it was.**

1. **Identify the runtime.** Worktree `rh-agents.worktrees/runtime-main` and its
   commit; every scheduler/launchd job and running `src.runner.main` /
   `src.scout` process (`launchctl list`, `ps`). Record them.
   *Go:* list complete.
2. **Database state.** `alembic current` on the runtime database (expected
   `0022`); `paused`, account row, open positions with their policy class
   (read-only SQL). *Go:* revision as expected; book understood.
3. **Stop every job.** Unload the scheduler jobs; wait until no run process is
   left (the deploy gate: never stop a scout mid-run without the check
   blocking). Confirm both advisory locks are free. If `paused=true`: read
   `control --pause-origin` only after 0024 (step 6) — before that, note it.
   *Go:* no process, no lock held.
4. **Backup and restore proof.** `pg_dump` of the runtime database; restore it
   into a scratch database; `alembic upgrade 0023` then `0024` and
   `alembic check` on the scratch copy; drop the scratch copy. *Go:* backup
   restorable, both migrations clean on the copy.
5. **Migration 0023** on the runtime database (`alembic upgrade 0023`). Check:
   both halts cleared, `paper_control_events` empty, `paused` unchanged.
   *Go:* checks pass.
6. **Migration 0024** (`alembic upgrade 0024`). Check: `paper_job_runs` empty,
   triggers present, `paused` unchanged, `alembic current` = `0024`.
   *Go:* checks pass.
7. **Security configuration.** New provider key in place, old key revoked (F);
   `TRADING_MODE=PAPER`; `COMMANDER_KILL_SWITCH` as intended; limits and exit
   policy values as approved; `PAPER_RUNNER_MARKET_ACQUISITION_ENABLED=true`;
   `PAPER_ENTRY_REQUIRES_EXIT_JOB=true`; `PAPER_EXIT_LIVENESS_MAX_AGE_SECONDS`
   larger than the exit job's interval plus its runtime. No secret printed.
   *Go:* every value confirmed by name, never by printing it.
8. **Deploy** the reviewed commit into the runtime worktree, only now. Run
   `--preflight`. *Go:* preflight exit code 0.
9. **Readiness.** One manual `--exits-once`; read its `paper_job_runs` row:
   `exit_ready=true`, `policy_classes` covering every open position's class,
   `uncovered_positions=0`. If paused: `--pause-origin`, then decide per the
   section above (separate approval). Then one manual `--entries-once`: its
   summary must show `exit_liveness=READY` (or a halt/pause that is intended).
   *Go:* exit evidence ready; entry job reads it.
10. **Scheduler** — only after explicit approval: exit job on a short interval
    (well under `PAPER_EXIT_LIVENESS_MAX_AGE_SECONDS`), entry job separately.
    Never `--once` and `--entries-once` both scheduled.
11. **Rollback.** Before step 5: nothing to undo. After 5/6 with old code: the
    schema is additive and old code never writes the new columns or lifts a
    pause, so its runs are unaffected — but its `--preflight` reports the schema
    as not current, which is expected and must be noted, not "fixed". Code rollback: redeploy the
    previous commit (the new tables stay). Schema rollback only if no halt, no
    event and no pause lift exists (both downgrades refuse otherwise), else
    restore the backup from step 4 with every job stopped.
12. **Restart after an incident.** Stop jobs; read `paper_job_runs` (latest
    rows, `RUNNING` rows past their bound), `control --status/--events`; resolve;
    start the exit job first, confirm `exit_ready=true`, then the entry job.
