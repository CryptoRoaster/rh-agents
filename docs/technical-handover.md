# RobinHood Agents: technical handover (interim)

State of `main` after PRs #115 and #116 (2026-10-19). This is an interim
handover for the next operator or developer; the full end documentation
follows once the remaining work is done. Nothing in this document has been
executed against the runtime: the runtime checkout still runs `cf25664`, no
migration beyond `0024` is applied there, no scheduler was changed, no
PAPER BUY and no LIVE trade happened.

## 1. What the system is

An autonomous on-chain trading system for Robinhood Chain (chain id 4663) in
**Phase 0: paper execution only**. Agents may request trades on their own;
every request passes the deterministic risk engine (SENTINEL), which no agent
can override; only the executor may ever touch signing infrastructure, and in
Phase 0 the executor is a paper simulator. PostgreSQL is authoritative for
positions and accounting. UNKNOWN or unavailable safety-critical data fails
closed. The ten invariants are in `AGENTS.md` and `ARCHITECTURE.md`.

## 2. Components

| Area | Code | Role |
|---|---|---|
| Contracts and config | `backend/src/core` | typed models, `Settings` (PostgreSQL `postgresql+asyncpg://` only), trusted clock, Decimal helpers |
| Persistence | `backend/src/data`, `backend/migrations` (head `0026`) | tables, repository, Alembic revisions |
| TradeCase workflow | `orchestration/workflow`, `orchestration/worker` | durable case state, evidence, blockers, SENTINEL bindings, leases (Phase 2A/2B) |
| Specialists | `agents/orbit`, `atlas`, `vector`, `pulse`, `anchor`, `signal`, `fuse`, `early` | ORBIT discovery (model), ATLAS on-chain facts and verdict (deterministic policy, model only comments), VECTOR/PULSE entry and trigger, ANCHOR liquidity and quotes |
| LP control | `agents/atlas/lpcontrol`, `agents/atlas/v4` | V4 census, pool index (#94), position control, fee credit, FeeSplitter templates; V2/V3 = `UNSUPPORTED` |
| Risk | `backend/src/risk` (`engine.py`, `authorization.py`) | SENTINEL: hard limits, `APPROVED`/`LIMITED`/`REJECTED`, UNKNOWN fails closed |
| Orchestration | `orchestration/commander`, `casefill`, `riskrequest`, `sizing`, `strategy`, `entryhalt`, `jobruns` | COMMANDER, kill switch and durable system pause, entry-only halt (0023), job-run evidence (0024) |
| Exits | `orchestration/exitpolicy`, `orchestration/paperexit` | auto and early exit policies, pre-exit refresh, one budget and one order per sweep, per-position isolation (#115), idempotent paper SELL by request key |
| Execution and ledger | `backend/src/execution/paper.py`, `backend/src/orchestration/paper.py`, `backend/src/ledger` | paper executor (refuses a BUY without established LP control); `PaperTradingService` (with case fill and risk request) refuses non-early BUYs in early-only mode; fills, cost basis, PnL |
| Markets | `backend/src/markets` | GeckoTerminal, KyberSwap quotes, observation recording, decimal precision |
| Runner | `backend/src/runner` | `python -m src.runner.main` with `--preflight`, `--exits-once`, `--entries-once`; `src.runner.control --halt-entries` |
| Scout | `backend/src/scout`, `ops/scout` | early discovery, watches, ORBIT budget, shadow runs; launchd today |
| Exit supervisor | `backend/src/supervisor`, `ops/exit-supervisor` (`linux/` = systemd) | 60 s exit tick, alert pass (Telegram), heartbeat sender, dead-man receiver |
| API and UI | `backend/src/api`, `frontend/` | GET-only FastAPI routes, Next.js dashboard |

## 3. How they work together

```
scout (5 min) ── young V4 pools ──► ORBIT review (budgeted) ──► early intake
        │                                                          │
        ▼                                                          ▼
pool index job (5 min) ──► ATLAS V4 census + pool discovery + fee credit + position control
                                                                   │
                                     SENTINEL (holders ≤ 35 % top ten, liquidity, slippage, 30 s freshness, LP control PASS)
                                                                   │
entry job (--entries-once) ── needs a ready exit run < 180 s ──► paper executor ──► ledger (PostgreSQL)
                                                                   │
exit supervisor tick (60 s, --exits-once) ──► exit policies ──► SENTINEL (sell side) ──► paper executor
        │
        ├─ paper_job_runs ──► exit liveness (EXIT_JOB_STALE gate for every BUY)
        ├─ alert pass ──► Telegram (single chat, rh-agents bot)
        └─ heartbeat ──► dead-man receiver on another machine
```

- **Entry and exit are separate jobs.** An entry needs a recent exit-job run
  that was `exit_ready=true` (`PAPER_ENTRY_REQUIRES_EXIT_JOB`,
  `PAPER_EXIT_LIVENESS_MAX_AGE_SECONDS` = 180); otherwise `EXIT_JOB_STALE`.
- **An exit sweep is isolated per position.** A refused, timed-out or failed
  position is counted and reported; the others still sell. A sale is keyed by
  a deterministic request key, so a retry or a restart never sells twice; an
  attempt whose outcome is unclear is settled by reading that key.
- **Stops.** `--halt-entries --reason` stops entries and keeps exits. The
  durable system pause (SENTINEL) and `COMMANDER_KILL_SWITCH` stop entries
  **and** exits; an unreadable pause ends the phase (fail closed).

## 4. Configuration

All settings come from the root `.env` (never printed, never committed;
`.env.example` has placeholders). Every capability is off by default.

| Group | Settings |
|---|---|
| Database | `DATABASE_URL` (`postgresql+asyncpg://`, database name required) |
| PAPER run | `PAPER_RUNNER_ENABLED`, `PAPER_EARLY_ONLY`, `PAPER_ENTRY_REQUIRES_EXIT_JOB`, `PAPER_EXIT_*` (liveness 180 s, run ≤ 60 s, attempts per run 5) |
| Early entry | `EARLY_SCOUT_ENABLED`, `ATLAS_FUNDING_GRAPH_ENABLED`, `PRE_VECTOR_EARLY_ENTRY_ENABLED`, `EARLY_PAPER_EXIT_ENABLED` |
| LP control | `ATLAS_V4_POOL_CONTROL_ENABLED`, `ATLAS_V4_POOL_INDEX_ENABLED` |
| Discovery | `EARLY_SCOUT_MAX_DISCOVERY_POOLS`, `..._MAX_NEW_WATCHES_PER_RUN`, `..._DISCOVERY_PAGE2_ENABLED`, `..._MAX_DISCOVERY_PAGE2_PER_DAY`, `..._V4_PREFILTER_ENABLED`, `..._MAX_ORBIT_REVIEWS_PER_RUN/_PER_DAY` |
| Stops | `COMMANDER_KILL_SWITCH`; durable pause and entry halt live in the database |
| Supervisor | Telegram and heartbeat secrets as systemd credentials only, never in the tick's environment |

The pilot values are in `docs/early-paper-pilot-runbook.md` (step 7 and 9)
and `docs/early-paper-readiness.md`.

## 5. How it is tested

In `backend/`: `uv sync --locked`, then

```
uv run pytest                                   # SQLite where a test builds its own engine
TEST_DATABASE_URL=postgresql+asyncpg://<user>@localhost/<disposable_test_db> uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

PostgreSQL is required for locking, concurrency and migration tests; SQLite
never substitutes for them. The frontend: `npm ci`, `npm run typecheck`,
`npm run lint`, `npm run build`. CI runs backend and frontend on every PR
(backend job bound 40 minutes; runner timeouts have needed a rerun).

Key acceptance suites:

| Suite | Proves |
|---|---|
| `tests/acceptance/test_early_paper_offline.py` | the whole early chain with production components; every unsafe LP-control case stopped at SENTINEL and at the executor |
| `tests/exitpolicy/test_isolation.py` | three positions, one failing (attempt or judgement), two sold, retry next sweep, booked-then-failed counted once, never sold twice; stop and cancellation never swallowed |
| `tests/runner/test_exit_job_faults.py` | the exit job with an unreadable stop, an unreachable database, a DB error inside one sale |
| `tests/paperexit/test_exit_acceptance.py` | UNKNOWN holders, provider timeout and a healthy position in one sweep; restart; exactly three exits |
| `tests/paperexit/test_unknown_integrity.py` | UNKNOWN integrity refuses that SELL with a typed code |
| `tests/supervisor/*`, `tests/ops/*` | alert and heartbeat decision tables, dead-man receiver, unit files and operator scripts against stand-ins |

## 6. Safe operation

- Runtime checkout `rh-agents.worktrees/runtime-main`, on `main`, clean;
  the supervisor wrapper refuses anything else. Never schedule from a
  development worktree.
- Deploy only a commit whose CI concluded `success`; respect the
  scout-unload gate and the deploy gate (no running scout, entry job or
  runner).
- Exit job before entry job, always. Entries only in early-only mode.
- Order of work, approvals and rollback: `docs/early-paper-pilot-runbook.md`.
- Lifting a system pause: `docs/paper-minimum-release-gate.md`.

## 7. Feature status

| Feature | State |
|---|---|
| TradeCase workflow, worker runtime, SENTINEL, ledger, paper executor | done, in production code |
| ORBIT, ATLAS (incl. funding graph), VECTOR, PULSE, ANCHOR | done, off by default |
| V4 LP control: census, pool index, position control, fee credit | done; index **not backfilled**, flags off |
| FeeSplitter verification | done offline; 13 live reads outstanding |
| Entry/exit split, exit liveness gate, entry-only halt | done |
| Exit isolation per position (#111) | done (#115) |
| Early-only mode, pre-VECTOR early entry, early exit policy | done, off |
| Scout with discovery profile | done; runtime runs an older profile |
| Exit supervisor (macOS launchd, Linux systemd), alerts, dead-man | code and tests done; **nothing installed on Linux, no receiver host** |
| Index job and entry job scheduler units | **missing** |
| V2/V3 LP control (#102), defense-in-depth binding (#98) | prepared / open |
| LIVE execution | not in Phase 0 |

## 8. Security boundaries

- No private key, seed phrase, signing library or live trading path.
- No agent signs, broadcasts or overrides a risk rejection.
- UNKNOWN fails closed, including the sell side (see #114).
- Runtime `.env` values are never printed; settings are confirmed by name.
- Public API routes are GET-only.
- The existing shared VPS is `VPS_NO_GO`; the Mac is not a 72-hour exit host.
- Every provider, RPC and model use is capped by a setting and needs a budget
  approval.

## 9. Exit safety acceptance (offline)

| Case | Evidence |
|---|---|
| Three open positions in one sweep | `paperexit/test_exit_acceptance.py`, `exitpolicy/test_isolation.py`, `runner/test_exits_once.py::test_an_exit_run_observes_all_five_early_positions` |
| One position fails, two others exit | `exitpolicy/test_isolation.py` (attempt and judgement, failure always first), `paperexit/test_exit_rpc_failures.py` |
| No double SELL | request-key replay and `POSITION_ALREADY_CLOSED` under the account lock: `exitpolicy/test_sweep.py`, `test_drive.py`, `test_isolation.py`, `paperexit/test_concurrency.py`, `runner/test_exit_phase.py` (PostgreSQL races) |
| Restart and retry | `test_exit_acceptance.py` (three sweeps, exactly three exits), `test_isolation.py`, `runner/test_exits_once.py`, `supervisor/test_tick.py` |
| Provider timeout | `EXIT_READ_UNAVAILABLE`: `test_exit_acceptance.py`, `test_exit_rpc_failures.py` |
| Missing holder data / UNKNOWN domain | typed `EXIT_RISK_REFUSED` (`HOLDERS_UNKNOWN`, `TOKEN_UNKNOWN`): `test_unknown_integrity.py`, `exitpolicy/test_sweep.py` |
| Unreadable database | `runner/test_exit_job_faults.py` (`DATABASE_UNAVAILABLE`, nothing sold; one sale's DB error costs that position only), `supervisor/test_models.py` (`DATABASE_UNREADABLE_POSITIONS_UNKNOWN` = CRITICAL) |
| Stop signal | `runner/test_exit_job_faults.py` (unreadable stop mid-sweep ends the run), `test_isolation.py` (stop and cancellation never swallowed), `exitpolicy/test_drive.py`, `entryhalt/test_runner.py` (halt keeps exits) |
| Exit alarm | `supervisor/test_alerts.py` (every non-healthy verdict, retries, token never leaked) |
| Dead-man heartbeat | `supervisor/test_deadman.py` (signed, replay-proof, restart-proof receiver), `jobruns/test_liveness.py` (180 s, `EXIT_JOB_STALE` at request and fill) |

Not proven offline, by design: delivery through real Telegram, a real
second host for the dead-man receiver, and 72 hours of continuous systemd
operation. These belong to the host acceptance (runbook step 8).

### Issue #114: may a SELL stay blocked on UNKNOWN?

Today SENTINEL refuses a sale on `HOLDERS_UNKNOWN`, `HOLDER_METRICS_UNKNOWN`
and `TOKEN_UNKNOWN`; the position counts as `blocked`, the run is not ready
(alarm, entries held), and every later run asks again.

- **For** blocking: invariant 8; a token whose contract cannot be read may
  not be sellable at the assumed price, so a booked PAPER fill could be
  fiction; consistent behaviour with the entry side.
- **Against** blocking: a sale only *reduces* exposure; holder concentration
  says nothing about whether a sale is possible; a holder provider outage or
  a rug would trap every position exactly when selling matters most.

Assessment (no policy change made):

1. **PAPER: keep as is.** No money is at risk, the block is visible
   (`EXIT_BLOCKED` alarm, entries held), and a fictitious PAPER fill would
   corrupt the evidence the pilot exists to collect.
2. **Before LIVE: split the codes.** `HOLDERS_UNKNOWN` and
   `HOLDER_METRICS_UNKNOWN` should no longer block a sale: they bear on entry
   risk, not on exit feasibility. `TOKEN_UNKNOWN` should keep blocking an
   automatic sale until the executor can bound it (minimum output from a
   fresh quote, simulated transfer); then it can be sold under that bound.
3. **Escalation either way:** a position blocked for more than N runs (for
   example 10 minutes) raises a separate critical alarm and asks for an
   operator decision; no automatic override.

This is a decision for the owner; it does not block the PAPER pilot.

## 10. Runtime readiness: GO / NO-GO (2026-10-19)

| Item | Status | Reason |
|---|---|---|
| 72-hour exit continuity | **NO-GO** | no host; Mac not continuous, VPS `VPS_NO_GO` |
| Isolated Linux host | **NO-GO** | not procured (out of scope of this handover) |
| systemd exit job | **NO-GO** (code ready) | units, scripts and tests exist; nothing installed |
| systemd index job | **NO-GO** | no unit exists yet |
| systemd entry job | **NO-GO** | no unit exists yet |
| Database availability | **NO-GO** | runtime PostgreSQL lives on the Mac; must move with the exit host |
| Alerting and dead-man | **NO-GO** (code ready) | no credentials provisioned, no receiver host |
| Backup and restore | **NO-GO** (procedure ready) | runbook step 2; never rehearsed on the runtime database |
| Migrations 0025/0026 | **NO-GO** (code ready) | runtime at `0024`; needs deploy approval |
| V4 pool index | **NO-GO** (code ready) | not backfilled; RPC budget approval needed |
| FeeSplitter evidence | **PARTIAL** | offline VERIFIED; 13 live reads outstanding |
| Discovery configuration | **GO (offline)** | profile verified by `tests/scout/test_target_profile.py`; runtime still on the old profile |
| RPC provider budget | **NO-GO** | estimates ready (≈ 4 000 / 13 000 backfill, ≈ 2 000 a day); not approved |

**Overall: NO-GO for the controlled early PAPER pilot.**

## 11. Release blockers for the controlled early PAPER pilot

| Blocker | Why it blocks |
|---|---|
| No isolated Linux host with 72-hour exit continuity (exit job, database, network path) | an early position lives up to 72 h; without continuous exits no entry may open |
| No dead-man receiver host and no provisioned alert credentials | a silent host must raise an alarm from elsewhere |
| Scheduler units for the index job and the entry job | the pilot needs both under systemd |
| Runtime deploy plus migrations 0025/0026 | the preflight refuses the new code until applied |
| Pool index backfill and its verification | without `MANAGER_HISTORY` no V4 PASS |
| FeeSplitter live reads (13) | template match on chain is UNKNOWN until read |
| Approvals: RPC budget, settings, schedulers, ORBIT budget, the pilot | operator decisions |

Not a blocker for PAPER, but must be decided before LIVE: #114 (SELL on
UNKNOWN holders), a worst-case treatment of claimable FeeSplitter token fees
(otherwise only launches read before their first sell can pass), #98.

## 12. Deferrable without endangering the goal

#30, #31 (Codex evaluation diagnostics), #93 (burn addresses in
concentration, documentation), #94 staged census (performance), #80 (typed
refusal instead of an IntegrityError on a second entry; the entry still
fails closed), #102 (V2/V3 stays `UNSUPPORTED`, fail closed), the remaining
#77 backlog items except the two scheduler units above, #40 (strategy
definition beyond the implemented early entry).

## 13. Next strategic stage: Solana (#104)

After a stable Robinhood early PAPER pilot. Not implemented, no code exists.
What it would need, so the architecture does not close the door:

- a chain abstraction beyond EVM (accounts, programs, SPL token and
  Token-2022 extensions such as transfer hooks and freeze authority);
- LP control for Solana AMMs (Raydium, Orca, Meteora, pump.fun migrations):
  LP-token burn, locker programs, position NFTs — fail closed until verified;
- holder and funding analysis over Solana accounts, mint and freeze
  authority as ATLAS domains;
- market data and quotes for Solana (provider-neutral port as today);
- the same invariants: paper first, SENTINEL unconditional, executor the only
  signer, UNKNOWN fails closed.
