# Operating a bounded PAPER run

Everything below is derived from the settings this code actually reads. No
secret, no key and no production address appears here or belongs here: values
live in the ignored root `.env`, and the placeholders are marked as such.

**Nothing on this page starts a run.** The preparation is described so that
starting one later is a deliberate, separately taken decision.

## 1. What a run is, in one paragraph

`python -m src.runner.main --once` performs **one** pass: it may acquire market
observations (if that is switched on), let intake open cases from recorded
candidates, let each configured specialist take the steps it can, ask SENTINEL
about whatever became ready, and fill what SENTINEL approved. It is bounded by
candidate, new-case, case, step and runtime budgets, it ends when nothing is
left that it may do, and it leaves no process behind. **A run that produces
nothing and says why is a successful run.**

## 2. Configuration, per capability

Set these in the root `.env`. Every one of them is off or unset by default, and
a capability that is switched on without what it needs fails at boot or is
reported as unavailable — never silently skipped.

### The run itself

| Setting | Meaning |
| --- | --- |
| `DATABASE_URL` | Required. `postgresql+asyncpg://…` with a database name. |
| `TRADING_MODE=PAPER` | Required for a run. `OBSERVE` is the default and refuses one. |
| `PAPER_RUNNER_ENABLED=true` | Consent for a run to exist. A run still only happens when somebody invokes it. |
| `PAPER_REQUESTED_NOTIONAL_USD` | The entry size, before fees, gas and slippage. Unset means nobody has said how large an entry should be, and that is a refusal rather than a default. |
| `PAPER_FEE_BPS`, `PAPER_SLIPPAGE_BPS` | The cost assumptions a simulated fill is priced with. Unset means *no cost basis*, not a free trade. |
| `PAPER_RUNNER_MAX_CANDIDATES`, `…_MAX_NEW_CASES`, `…_MAX_CASES`, `…_MAX_STEPS`, `…_MAX_SECONDS`, `…_STEP_TIMEOUT_SECONDS` | Upper bounds, never targets. A single step may not be allowed to outlast the run. |
| `COMMANDER_KILL_SWITCH=true` | A local stop. A run refuses to start while it is set. |

### Specialists

Each role is `false` by default. Switching one on without what it needs is
reported by the preflight as a blocked role, and the executing CLI refuses the
run for the same reason.

| Role | Needs |
| --- | --- |
| `ORBIT_WORKER_ENABLED` | `REASONING_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`. |
| `ATLAS_WORKER_ENABLED` | `EVM_RUNTIME_ENABLED=true`, exactly one enabled chain, and the holder/origin providers with their keys. |
| `SIGNAL_WORKER_ENABLED` | A reasoning provider and `SIGNAL_SOCIAL_PROVIDER` with its key. |
| `VECTOR_WORKER_ENABLED` | A reasoning provider and `VECTOR_HISTORY_PROVIDER` matching `MARKET_PROVIDER`; exactly one chain. |
| `PULSE_WORKER_ENABLED` | Nothing beyond recorded markets. |
| `ANCHOR_WORKER_ENABLED` | `EXECUTION_QUOTE_PROVIDER=kyberswap` for executable quotes; without one it establishes no capacity, which is the correct outcome rather than a guess. |
| `FUSE_WORKER_ENABLED` | Nothing: it synthesises verdicts the others already committed. |

**One chain at a time for ATLAS and VECTOR.** Their ports are bound to a single
chain at construction, so with `MARKET_CHAINS=robinhood,bsc` both report
`…_CHAIN_AMBIGUOUS` rather than serving one chain and silently refusing the
other.

### Market acquisition (optional)

Off by default and separate from the run switch, because consenting to a run is
not consenting to that run calling a public API.

| Setting | Meaning |
| --- | --- |
| `PAPER_RUNNER_MARKET_ACQUISITION_ENABLED=true` | Requires `PAPER_RUNNER_ENABLED=true` and `MARKET_PROVIDER=geckoterminal`. |
| `PAPER_RUNNER_ACQUISITION_MAX_MARKETS` | Distinct case and discovery markets one run may observe again. Open positions do not use it. |
| `PAPER_RUNNER_ACQUISITION_MAX_POSITION_MARKETS=20` | Distinct open-position markets observed again first, in their own budget (one bounded `pools/multi` request carries up to 20). |
| `…_MAX_DISCOVERY_REQUESTS` | Bounded discovery reads. `0` acquires only what open work depends on. |
| `…_MAX_PROVIDER_REQUESTS`, `…_MAX_HTTP_ATTEMPTS` | Applied to the provider's own budgets, so they can only tighten them. |
| `…_MAX_SECONDS` | The stage, additionally bounded by the run's remaining time. |

Without acquisition a run trades only what `python -m src.markets.ingest --once`
or a market watcher has already recorded.

**Reading the acquisition account.** `budget_spent` is capacity this run
committed before asking — for a discovery read, the size of the answer it was
permitted to return — and `recorded` is what was durably written. Neither says
what the provider delivered, and `budget_spent=2` with `recorded=1` is **not**
"two pools offered, one kept". For that, read the `discovery` block: one entry
per read, naming its chain, the capacity it `reserved`, how many pools the
adapter `considered` and `returned`, and how many it `rejected` under which
fixed codes. A read that did not return says `completed=false` with a reason and
reports no counters, because it never finished counting. How many pools the
provider's document carried is not counted anywhere and is not inferred.

**Reading the position coverage.** `acquisition.positions` says how much of
the open portfolio this pass observed again: `open_positions`, the distinct
full market identities behind them (`markets`), the holdings whose market
cannot be asked about at all (`unaddressable`: never recorded, no pool locator,
another provider or chain), and of the rest how many were `asked`, `answered`
(the provider answered and the recorder accepted it, new or replayed),
`refused` (not returned, or naming another market), `failed`, `unknown` and
`not_attempted` (`POSITION_CAPACITY_EXCEEDED`, a budget or time stop). It is
coverage, not freshness: an answered reading that is unavailable or old is
still unusable for every reader that judges age. The identity asked for is the
case's — position, cycle, entry, case — so a newer reading of another market
under the same pool id can never redirect the request. Positions are observed
before anything else and the exit sweeps run after the stage.

### Pre-risk market refresh (with acquisition)

Composed whenever acquisition is enabled, and never otherwise. SENTINEL refuses
market observations older than its own 30-second bound, and the run-start
acquisition has aged past that by the time a case has waited on its handlers.
So immediately before a **new** risk request the run observes again, by exact
pool locator only, the markets that request will read:

- the case's own market, and
- the market of every open position SENTINEL values,

each once. No discovery, no other case, and no separate payment-asset market:
a version-3 observation of the case's pool carries the quote asset's USD price
ANCHOR converts with, so refreshing the pool refreshes that price too. Each market must come back as exactly the stored identity (provider,
chain, network, venue, both assets, pool locator); nothing is searched by
symbol, name or address and no pool is substituted. Observations are recorded
through the ordinary recorder and committed before the request.

**Pair-agnostic.** Any pair the market path supports is refreshed alike —
MEME/MEME, MEME/TOKEN, TOKEN/WETH, TOKEN/stable, native/token, token/native.
Base and quote are taken unchanged from the stored identity; no quote asset is
required, preferred, ranked or excluded, and nothing here names WETH, WBNB or a
stablecoin.

| Setting | Meaning |
| --- | --- |
| `PAPER_RUNNER_PRE_RISK_MARKET_MAX_REQUESTS=3` | Provider requests per refresh, network resolution included. Its own budget, never the acquisition's. |
| `PAPER_RUNNER_PRE_RISK_MARKET_MAX_SECONDS=15` | Time per refresh, additionally bounded by the run's remaining time. |

**Network resolution is validated once per run.** GeckoTerminal's `/networks`
list is paginated and Robinhood is on its third page, so validating it costs
three requests — as much as the whole pre-risk budget. Each PAPER run therefore
keeps a run-scoped `VerifiedNetworkRegistry` (chain → provider network id, and
nothing else). The first resolution of a chain in the run goes through the
ordinary `NetworkDirectory.resolve()` validation — paginated list, configured
id, platform binding, contract checks — and only a successful validation is
remembered. The run-start acquisition normally fills it, so each pre-risk
refresh, including one after a source refresh, costs only its exact pool
batches: one per chain (Robinhood alone 1 request, Robinhood + BSC 2). A chain
not yet validated in the run is validated by the refresh itself under its own
budget and fails closed when that budget ends. Failures are never remembered,
the registry is not persisted, and the next run starts empty. Each refresh
reports `network_resolution_cache` and `network_resolution_provider`.

The order is: market refresh → risk request; and when that request is refused
for a stale ATLAS/ANCHOR source, source refresh → market refresh again → the
same request re-asked. The existing source-refresh bound applies; no source
refresh means no second market refresh. A `RISK_APPROVED` replay is never
refreshed — its verdict already exists.

**Fail closed.** If any needed market cannot be shown fresh, no risk request is
sent and the case's progress says why in `pre_risk_refusal`:
`MARKET_IDENTITY_UNKNOWN`, `POOL_LOCATOR_UNKNOWN`, `CHAIN_NOT_CONFIGURED`,
`MARKET_NOT_RETURNED`, `MARKET_IDENTITY_MISMATCH`, `PROVIDER_FAILED`,
`TIME_BUDGET_REACHED`, `REQUEST_BUDGET_REACHED`, `RECORD_OUTCOME_UNKNOWN`,
`MARKET_STILL_STALE` or `DATABASE_UNAVAILABLE`. An older observation is never
used in place of a failed read, and a replayed provider event is judged on the
source time actually stored — nothing is re-dated. A liquidity the provider
could not state stays unknown, and the request's own readiness check refuses it.

**Reading it.** Three things are reported apart: the run-start `acquisition`
block, each case's `refreshes` (workflow source refreshes of ATLAS/ANCHOR), and
each case's `market_refreshes` — one entry per pre-risk refresh, marked
`stage=PRE_RISK_MARKET_REFRESH`, with `ready`, `reason`, `required_markets`,
`attempted`, `recorded`, `unchanged`, `refused`, `failed`, `provider_requests`
and the canonical pair ids. The run summary adds `pre_risk_market_refreshes`
and `pre_risk_refusals`. Counts and codes only, never a provider payload.

### The exit job (`--exits-once`) and run locks

`python -m src.runner.main --exits-once` is one bounded pass of the **exit job**:
the market acquisition restricted to open positions' markets (no case market,
no discovery read), then the `PAPER_EXIT_V1` and `EARLY_PAPER_EXIT_V1` sweeps,
and nothing else — no promotion, intake, worker step, risk request or BUY. It
runs only in PAPER, refuses with `NO_EXIT_SWEEP_CONFIGURED` when neither exit
policy is configured, and is held to `PAPER_EXIT_RUN_MAX_SECONDS` (default 60).
`--once` is the **entry job** and is otherwise unchanged.

Each job holds its own PostgreSQL session-level advisory lock for the whole run
(`rh-agents:paper-exit-job`, `rh-agents:paper-entry-job`), taken without
waiting. A second start of the same job ends with `stop=ALREADY_RUNNING`,
`lock=ALREADY_RUNNING`, asks nobody and writes nothing. A crashed process drops
its connection and PostgreSQL releases the lock; there is no lease row. The two
jobs do not exclude each other: every fill and exit takes the paper account
row first, orders are keyed and the database allows one exit per cycle, so a
second lock would only add a lock order and let an entry run delay a stop.

Every summary now carries `mode` (`FULL` / `EXITS_ONLY`), `lock`
(`ACQUIRED`, `ALREADY_RUNNING`, or `NOT_SUPPORTED` on the SQLite test engine)
and `duration_seconds` (monotonic). No scheduler is installed by any of this.

## 3. The preflight

```sh
cd backend
uv run python -m src.runner.main --preflight
```

It writes nothing, calls nobody, and prints one JSON object. Every check carries
a status:

- **`SATISFIED`** — asked locally and met.
- **`BLOCKED`** — asked locally and in the way. This is what to go and fix.
- **`NOT_CHECKED`** — answering would mean calling somebody. A configured key is
  reported as *configured*, never as valid; a selected provider as *selected*,
  never as reachable.
- **`UNAVAILABLE`** — the check could not be carried out: the database did not
  answer inside `PAPER_RUNNER_STEP_TIMEOUT_SECONDS`, or the check could not be
  started or was interrupted. **Not the same as "not ready"** — it means nobody
  can currently tell, and it is what produces exit `1`.

Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Everything checkable locally is in place. |
| `2` | Something is missing. A configuration statement, not an outage. |
| `1` | A check could not be carried out. Go and look at the machine. |

What it checks: whether a run is permitted at all (mode, runner switch, kill
switch), the configured chains, the run and acquisition budgets, every role and
whether this configuration can compose it, the database's **whole** recorded
revision set against the migration head this code ships with — an extra
revision beside the expected one is refused, never ignored — and the durable
account pause. `/ready` answers the schema question through the same contract,
so the two cannot disagree.

**A green preflight is not an authorization.** It describes a moment that has
already passed. The executing CLI still runs its own refusals, SENTINEL still
judges every request, freshness and execution bounds still apply, and a stop
committed one second later still stops the run.

## 4. A conservative first run, to be released separately

The smallest thing that can happen is a pass that opens at most one case and
takes few steps. Stated here for review; **do not run it as part of preparing
it.**

```sh
# root .env — placeholders, never real values in this repository
TRADING_MODE=PAPER
PAPER_RUNNER_ENABLED=true
PAPER_REQUESTED_NOTIONAL_USD=25
PAPER_FEE_BPS=30
PAPER_SLIPPAGE_BPS=50
PAPER_RUNNER_MAX_CANDIDATES=1
PAPER_RUNNER_MAX_NEW_CASES=1
PAPER_RUNNER_MAX_CASES=1
PAPER_RUNNER_MAX_STEPS=12
PAPER_RUNNER_MAX_SECONDS=120
PAPER_RUNNER_STEP_TIMEOUT_SECONDS=30
PULSE_WORKER_ENABLED=true
FUSE_WORKER_ENABLED=true
```

```sh
cd backend
uv run alembic upgrade head            # once, and only when a migration is due
uv run python -m src.runner.main --preflight
# only after that reports exit 0, and only as a separate decision:
uv run python -m src.runner.main --once
```

With the specialists above and no reasoning provider, such a run can open a case
and will not reach a fill — which is the point of a first one. Adding roles adds
external calls and cost, one at a time.

## 5. What a run can leave behind

A run is **not** one transaction, and that is deliberate: a crash at the end
must not discard a fill that really happened.

- **Confirmed partial work stays.** Cases opened, evidence recorded, market
  observations written and a stored risk verdict all remain if a later stage
  fails. The summary reports what committed rather than what was hoped for.
- **Unknown outcomes are named.** A decisive call cut off mid-flight may have
  committed and may not. The run marks that case `outcome_unknown` instead of
  guessing, and stops before doing anything further with it.
- **An unknown acquisition outcome ends the pass** before any further mutating
  trading stage, for the same reason.

## 6. Running again after an interruption

Just invoke `--once` again. Identity is what makes that safe, and all of it
already exists:

- the **intake key** is scoped to the market *generation*, so a re-run converges
  on one case rather than opening a second;
- the **order key** comes from the case, so a second run addresses the same
  order and replays the stored verdict instead of minting a new one;
- an **execution** is unique by intent and bound to its case;
- the **recorder** is idempotent per event, so a re-observed market is a new
  event and never a rewritten one;
- a **lease** left behind by an interrupted worker expires and is recovered
  rather than being marked failed by a process that stopped watching it.

A replay needs no new acquisition: a stored verdict is answered from history.

## 7. Limits

- A run guarantees **no case reaches readiness** and **no fill happens**. Both
  are outcomes of evidence and risk, not of invoking anything.
- A preflight makes no statement about reachability, credential validity or
  market freshness, and none of those can be established without an external
  call this system deliberately does not make outside a run.
- Missing local preconditions are named by the preflight; missing *external*
  ones only ever surface during a run, as refusals.
- No daemon, scheduler, API background start, automatic exit or re-entry, live
  trading, signing, broadcast or Docker.
