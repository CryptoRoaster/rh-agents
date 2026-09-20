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
| `PAPER_RUNNER_ACQUISITION_MAX_MARKETS` | Distinct markets one run may observe again. |
| `…_MAX_DISCOVERY_REQUESTS` | Bounded discovery reads. `0` acquires only what open work depends on. |
| `…_MAX_PROVIDER_REQUESTS`, `…_MAX_HTTP_ATTEMPTS` | Applied to the provider's own budgets, so they can only tighten them. |
| `…_MAX_SECONDS` | The stage, additionally bounded by the run's remaining time. |

Without acquisition a run trades only what `python -m src.markets.ingest --once`
or a market watcher has already recorded.

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
