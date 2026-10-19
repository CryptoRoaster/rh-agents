# Local observation on the Mac

A local, observation-only operating mode: real pools are discovered and real
market data is recorded and evaluated, potential early candidates are listed,
and no position is ever opened. It runs the early-discovery scout
(`python -m src.runner.main --scout-once`) every 300 seconds against an
isolated database, under launchd, from a dedicated checkout.

Nothing here is a trading path. The scout never forms a trade case, an order
intent or a position (`backend/src/scout/service.py` module docstring;
`tests/scout/test_cycle.py::test_the_scout_never_opens_a_trade_case`).

## 1. What runs, and what cannot

| Part | State in the observation profile |
| --- | --- |
| Discovery (GeckoTerminal `new_pools`) | on: all 20 pools of page 1; page 2 only on a saturated, non-overlapping page 1, at most 72 a day |
| V4 / V2 / V3 | V4 prefilter on: V4 pools are prioritised for review; V2/V3 pools are still recorded and watched |
| Deduplication | database unique keys per pool and checkpoint; the database is the overlap reference across restarts |
| VECTOR market history (GeckoTerminal OHLCV) | on: at most 6 reads a run, 6 s apart |
| ORBIT model review | off: 0 a run, 0 a day, until a model budget is approved |
| JEV shadow triage | off: `FAST_REASONING_PROVIDER` is not in the profile |
| ATLAS, SENTINEL, funding graph | not run: they exist only inside a PAPER run, which this profile cannot start; their facts stay UNKNOWN |
| RPC | off: `EVM_RUNTIME_ENABLED` is not in the profile (default false) |
| PAPER run, entries, exits | refused: `TRADING_MODE=OBSERVE`, `PAPER_RUNNER_ENABLED=false` (`run_configuration_refused` / `PAPER_RUNNER_NOT_ENABLED`) |
| LIVE trading | does not exist: `TRADING_MODE` allows only `OBSERVE` or `PAPER` |

The guarantees are enforced in `ops/observe/common.sh` before every start and
every run:

- The profile file is read, never sourced. Only allowlisted keys are accepted;
  any other key is refused (`OBSERVE_ENV_KEY_NOT_ALLOWED_*`). This covers every
  trading gate, every provider key and every RPC URL.
- These values are fixed: `TRADING_MODE=OBSERVE`, `PAPER_RUNNER_ENABLED=false`,
  `EARLY_SCOUT_ENABLED=true`, `MARKET_PROVIDER=geckoterminal`,
  `MARKET_CHAINS=robinhood` and `REASONING_PROVIDER=codex`.
- `DATABASE_URL` must name a local `rh_agents_observe[_suffix]` database and
  carry no password (`OBSERVE_DATABASE_NOT_ISOLATED`).
- ORBIT reviews are explicit numbers and stay at 0 unless
  `RH_AGENTS_OBSERVE_MODEL_BUDGET_APPROVED=yes`
  (`OBSERVE_MODEL_BUDGET_NOT_APPROVED`). An approved budget is still capped at
  4 a run and 96 a day (`OBSERVE_MODEL_BUDGET_ABOVE_CAP`).
- The scout starts with `env -i` plus the profile. No inherited variable and no
  wrapper key reaches it. A repository `.env` is refused
  (`OBSERVE_REPOSITORY_ENV_PRESENT`), because settings would read it.
- Every script first re-executes itself under `env -i` unless only `HOME`,
  `PATH`, `USER`, `LOGNAME`, `TMPDIR`, `LANG`, `TERM` and the scripts' own
  `RH_AGENTS_OBSERVE_*`, `RH_AGENTS_LAUNCH_AGENTS` and `RH_AGENTS_PSQL` knobs
  are exported and no shell function is defined. An inherited `BASH_ENV`,
  exported function, `GIT_*` or `PG*` variable therefore never reaches the
  checks below.
- Only committed code runs. Refused (`OBSERVE_WORKTREE_MODIFIED`):
  - a tracked change;
  - an untracked or ignored file under `ops/` or `backend/`, except the
    virtualenv and tool caches. `python -m` puts `backend/` first on `sys.path`.

  `start.sh` also requires a commit on `origin/main`.

## 2. Setup (once)

1. Use a dedicated checkout on a commit of `origin/main`, never the
   development checkout or the runtime worktree. Build its virtualenv with
   `cd backend && uv sync --locked`.
2. Create the isolated database and migrate it. These are the only migrations
   this mode performs:

   ```bash
   createdb rh_agents_observe
   cd backend
   DATABASE_URL=postgresql+asyncpg://$USER@localhost/rh_agents_observe .venv/bin/python -m alembic upgrade head
   DATABASE_URL=postgresql+asyncpg://$USER@localhost/rh_agents_observe .venv/bin/python -m alembic check
   ```

3. Copy `ops/observe/observe.env.example` to `~/.config/rh-agents/observe.env`
   (mode 0600) and replace `YOUR_ROLE`.
4. Check the profile and the configuration without any call:

   ```bash
   ops/observe/run-observe.sh --check       # the profile and the checkout
   ops/observe/run-observe.sh --preflight   # the runner preflight under the profile
   ```

   The preflight writes nothing and calls no provider. It must show
   `RUN_PERMITTED` as `BLOCKED` (`PAPER_RUNNER_NOT_ENABLED`) and
   `DATABASE_SCHEMA` as satisfied.

Backup and restore of the runtime database are proven separately and
read-only, with `ops/db/rehearse-migration.sh` (section 4 of
`docs/runtime-host-readiness.md`). The observation database starts empty and
needs no copy of runtime data.

## 3. Operation

| Action | Command |
| --- | --- |
| Start (needs explicit approval) | `ops/observe/start.sh --confirm` (`--dry-run` prints the agent and changes nothing) |
| Health / status | `ops/observe/status.sh` (exit 0 healthy, 1 degraded, 2 trading state found or unreadable) |
| Stop | `ops/observe/stop.sh`: sets the STOP file, waits for the run in progress (at most 600 s), then unloads the agent |
| Emergency stop | `ops/observe/stop.sh --now`: as above, but terminates the run in progress |
| Restart | `ops/observe/start.sh --confirm`: clears STOP; the next run continues from the database |
| Logs | `~/Library/Logs/rh-agents/observe.log` (rotated at 5 MB, three generations) |

- **Starting.** The first run begins one interval after loading
  (`RunAtLoad` is false).
- **Overlap.** Runs never overlap:
  - launchd does not start a job that is still running;
  - the wrapper takes an atomic lock directory and records its pid and start
    time; a pid left behind by a crash is trusted only while the same process
    (same pid, same start time) is still a `run-observe.sh`;
  - the scout holds a PostgreSQL advisory lock.
- **Stopping without data loss.** Every stage commits on its own. A run that is
  terminated loses only its uncommitted stage, and the next run records the
  same pools again idempotently (`ON CONFLICT DO NOTHING`, unique keys per pool
  and checkpoint).
- **Status.** `status.sh` lists the latest runs, the provider requests and
  model reviews of the current day, watches by status, and the early
  candidates (`PROMOTABLE`: VECTOR-sufficient history, not an approval of
  anything). It also checks the invariants
  `trade_cases = order_intents = positions = 0`.

## 4. Mac requirements

The Mac must stay awake. `pmset` shows `sleep 0` on AC and on battery. A
closed lid without an external display still sleeps the machine: during a
sleep no run happens, and status reports `NO_RECENT_RUN`. Keep the Mac on AC
power.

## 5. Providers and budget

| Provider | Needed for | Credential | Calls in this profile | Cost |
| --- | --- | --- | --- | --- |
| GeckoTerminal public API v2 | discovery, refreshes, OHLCV | none | at most 5 logical market requests a run, hard-capped at 8 HTTP attempts with retries (`GECKOTERMINAL_MAX_HTTP_ATTEMPTS`); plus at most 6 OHLCV reads, 1 retry each; worst case 8 + 12 = 20 HTTP calls a run, 5 760 a day; expected 1 to 6 a run | free; public rate limit about 30 calls a minute (provider documentation, NOT VERIFIED here), far above ≤ 20 per 5 minutes |
| Codex (ORBIT) | model review of watches | local Codex login | 0 (budget 0) | per call NOT VERIFIED (subscription); needs approval |
| Anthropic | alternative ORBIT model | not in the profile | 0 | needs a key and approval |
| TypeSafe JEV | shadow triage | not in the profile | 0 | needs approval |
| Robinhood chain RPC, Blockscout, NodeReal, Etherscan | ATLAS, V4 pool index, funding | not in the profile | 0 | not part of this mode |

**Proposed ceiling for activation:**
- GeckoTerminal at most 5 760 HTTP calls a day (worst case; the expected figure is about 300 to 1 700), all other providers 0, cost 0.
- An ORBIT budget, if wanted, is a separate approval. The code defaults are 4 a run and 96 a day. Set them together with `RH_AGENTS_OBSERVE_MODEL_BUDGET_APPROVED=yes`.

## 6. Verification

`backend/tests/ops/test_observe_local.py` covers:
- the command (`--scout-once` only) and the clean environment;
- every refusal;
- the STOP file, overlap, TERM handling, stop/wait/timeout/`--now`, start and the agent's interval;
- status and its invariant check.

Discovery, deduplication, restart, ATLAS fail-closed and missing RPC data as
UNKNOWN are covered offline by the existing suites:
- `tests/scout`
- `tests/atlas`
- `tests/early`
- `tests/entryhalt`
- `tests/markets/test_recorder.py`
- `tests/runner/test_integration.py`
