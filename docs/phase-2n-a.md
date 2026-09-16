# Phase 2N-A — Bounded PAPER run

One explicitly started pass that joins components that already existed but had
never been connected to each other by anything runnable:

recorded market candidates → intake → specialist tasks → canonical risk request
→ case-bound PAPER fill — as far as the existing contracts and the current data
actually allow.

A run may end with waiting or blocked cases. **A successful run promises no
fill.** No automatic exit or re-entry, no daemon, scheduler, background start,
public write endpoint, live trading, signing, broadcast or Docker.

## What was already connected, and what was not

Read before anything was written.

**Executable and connected:** `MarketReader` over recorded observations;
`CommanderIntakeService.run_cycle()`; `TradeCaseService` (status owner, creates
role tasks at open); `WorkerRuntimeService` with claims, leases and attempts;
`WorkerRunner.run_once()`; seven specialist handlers with their context readers;
`RiskRequestService` → `src.risk.engine.evaluate` → a stored binding;
`CaseFillService` → `PaperExecutor` → the ledger; `AccountPauseReader` for the
durable stop.

**Not connected:** *nothing ran any of it.* The API composes no worker and
starts no task — `WORKER_RUNTIME_ENABLED` was read only by a read-only endpoint —
and there was no entry point that carried a case from a candidate to a fill.

**Ports still missing, named rather than faked:**

- `reasoning_provider="fake"` is **not constructible from configuration** and
  never will be: `DeterministicReasoningProvider` replays a script, and a
  configuration has no script to give it. Only `anthropic` composes. This is the
  guarantee that no production configuration can reach a synthetic model.
- **ATLAS** reads contract facts through the EVM runtime, a separate process
  with its own lifecycle. Composing one inside a trading run would start chain
  I/O from inside it, so the port is supplied or the role is absent.
- **Market history** for VECTOR is not composable here.
  `GeckoTerminalOhlcvSource` needs a transport whose lifetime is an async
  context manager and one chain fixed at construction, while a run handles
  whatever chains its cases are on. Wiring it properly is real work this phase
  does not do, and the role is reported `MARKET_HISTORY_NOT_COMPOSABLE` rather
  than quietly downgraded to the unconfigured source.

**Where idempotency and restart were already secured:** the intake key is scoped
to the market *generation*, and `open_trade_case` is idempotent on it at the
database level; worker claims, leases and attempts are durable; a risk request is
unique per case and replays by key; an execution is unique by intent id and bound
to one case; replay is checked before every authorization check. This phase adds
none of that and relies on all of it.

**What this one run may trigger:** an intake cycle, specialist task steps for
roles that are configured *and* composable, one risk request per ready case, and
one fill per approved request. Nothing else. No exit, no re-entry, no ingestion.

## The entry point

```
uv run python -m src.runner.main --once
```

`--once` is required and is the only mode. There is no interval, no daemon flag
and no scheduler. The API never reads `PAPER_RUNNER_ENABLED` and starts no task,
so booting the web process can never begin a run.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The run completed. Cases may be waiting, refused or filled; the summary says which, and reaching a budget still counts as completed. |
| `1` | A technical failure — the database was unreachable, a service raised, the process was interrupted. Never a statement about a market. |
| `2` | The configuration does not permit a run. Detected before any mutating step; nothing was attempted and nothing was written. |

A business refusal — SENTINEL declining, evidence missing, a case waiting — is
**not** an error exit. The run did what it exists to do and reported a stop.
Conflating that with `1` would teach an operator to ignore the one code that
means something is actually broken. Waiting, refusal and fault are told apart in
the summary: `stop`, the per-case `risk_refusal` / `fill_refusal` / `risk_outcome`
fields, and `errors`.

## Configuration and budgets

| Setting | Default | What it bounds |
| --- | --- | --- |
| `PAPER_RUNNER_ENABLED` | `false` | whether a run may exist at all |
| `PAPER_RUNNER_MAX_CANDIDATES` | `5` | candidates one pass may take from intake |
| `PAPER_RUNNER_MAX_STEPS` | `40` | specialist steps in one pass |
| `PAPER_RUNNER_MAX_CASES` | `3` | cases one pass may decide |
| `PAPER_RUNNER_MAX_SECONDS` | `300` | the whole pass |
| `PAPER_RUNNER_STEP_TIMEOUT_SECONDS` | `60` | one external wait inside it |

Every one is an upper limit, never a target. Refused at settings level:
`PAPER_RUNNER_ENABLED` without `TRADING_MODE=PAPER`, and a step timeout longer
than the run — a single wait that may outlast the run is not a bound. Provider
budgets are the existing ones (`GECKOTERMINAL_MAX_REQUESTS`,
`ATLAS_HOLDER_MAX_PAGES`, the per-source timeouts); this phase adds no second
budget beside them.

A run also needs what every entry already needed and does not default:
`PAPER_REQUESTED_NOTIONAL_USD`, `PAPER_FEE_BPS`, `PAPER_SLIPPAGE_BPS`. Unset
means there is no entry size and no cost basis, which is a refusal.

## Responsibilities, unchanged

COMMANDER stays read-only and gains no setter, no execution right and no way
around a blocker. The run calls public service contracts and owns no rule:
status belongs to the workflow, task handling to the runtime, sizing to the
sizing contract, verdicts to SENTINEL, money to the ledger.

Only a current `APPROVE` with `APPROVED` authorization reaches
`CaseFillService`. `LIMITED`, `REJECT`, `PAUSE_SYSTEM`, incomplete data, stale
evidence and missing marks are stops. There is no second key for a final
verdict, no deadline extension, no downsize and no way around the short approval
window.

## Idempotency and restart

The order key is `paper-run:<trade_case_id>` — derived from the case, which
survives a restart, and never from the run id or the clock, which do not. The
risk request and the fill use the same key, because they are two halves of one
order.

`run_id` exists so one pass can be found in logs. **It is not a trading
identity** and nothing is derived from it.

A restart finds stored results rather than repeating them: a case already at
`RISK_APPROVED` replays its stored verdict under the same key and the fill is
attempted once — and if the short window has since closed, the fill refuses
rather than being granted an extension.

**A run is not one transaction.** Steps that committed stay committed; the
interrupted step follows its own contract — a claimed task keeps its lease until
it expires and recovery reclaims it, a fill either committed or rolled back
entirely. Wrapping the pass in one transaction would mean a crash at the end
discarded a fill that really happened.

**A run never waits for work to appear.** When no role can claim a task, the
pass ends. Work that is not due yet stays in the task table — the loop lives
there, durably, not inside a process somebody has to keep alive. No sleep, no
poll, no busy wait. Provider calls happen inside handlers, never inside a
booking transaction.

## Operational output

One JSON object: `run_id`, limits, per-role availability with a reason when a
role could not run, candidates seen, cases opened, intake refusal codes, steps
taken, per-case progress (status, reason code, risk outcome, risk refusal, fill
refusal, execution id, replayed), counts of requests, fills and replays, and
technical `errors` as codes.

Safe by construction: every value is an identifier this system already exposes,
a count, or a typed reason code from a published vocabulary. No secret, provider
payload or exception text is ever put into the model, so none can be printed.

## Test evidence

30 tests in `tests/runner/`, on a schema carrying markets, workflow and
accounting.

**Real components:** the real intake, the real workflow service, the real worker
runtime and `WorkerRunner`, the real `MarketRecorder` and `MarketReader`, the
real `RiskRequestService`, `src.risk.engine.evaluate`, the real `PaperExecutor`,
the real ledger, and the real `build_stack` composition. The run object under
test is the production one.

**Fixtures:** the recorded market observations are fixture-shaped rows written
through the real recorder; the case evidence is fixture evidence submitted
through the real workflow, because no specialist could produce it here.

**Mocked ports:** none needed for the paths proved. Two handlers are substituted
in the bounds tests — one that never returns, one that reports a wait — to
exercise the step timeout and the no-spin ending. `RunnerPorts` is passed empty,
so no external port exists at all.

**Real external calls: none.** No provider, no model, no network. No run in this
suite lasts beyond the call that asked for it.

Covered: a recorded candidate opening a case and the pass ending rather than
waiting; a ready case going all the way to a booked fill with real cash and fee
movement; the order key derived from the case and not from the run; a second run
replaying instead of ordering again; an interruption between the verdict and the
fill leaving the order addressable and the next run completing it once; a
failure inside the fill leaving no partial booking, with a later run still
completing it once; two concurrent runs producing one order and one fill
(PostgreSQL only); a paused account stopping the run before it opens anything;
the kill switch refusing before any mutation; `OBSERVE` and an over-long step
timeout refused in the settings; no market data producing an honest empty pass;
stale market data and a missing entry size each preventing the fill; the time
and case budgets ending the pass; an external call that hangs bounded by the
step timeout; a waiting monitor ending the pass without spinning; a
configuration never producing a synthetic model; a role with an unbuildable port
reported rather than skipped; `--once` required; an invalid configuration
exiting `2` without touching anything; the summary carrying nothing but codes
and counts; no background task left behind; and no exit, re-entry or second
case for an executed market.

Full gates: PostgreSQL 3288 passed / 20 skipped, SQLite 3204 passed / 104
skipped, ruff, strict mypy, Alembic at `0011` with `check` clean and offline SQL
generated, frontend typecheck/lint/format/build.

**No migration.** This phase persists nothing of its own: a run is an ordering
of calls into services that already own their tables, and a run id that is not
an identity has nothing to store.

## Remaining limits

- **A run promises nothing.** Waiting and blocked cases are ordinary outcomes.
- **ATLAS and VECTOR history are not composable from configuration**, so those
  roles report unavailable in a production run. Wiring them is future work, and
  naming it is better than a stub that looks like a specialist finding nothing.
- **Only `anthropic` composes as a reasoning provider**, by design.
- **No automatic exit or re-entry**, and no automatic stop-loss or take-profit.
- **No daemon, scheduler or background start.** One pass per invocation.
- **No public write endpoint.** The API remains read-only.
- Live execution, signing, broadcast and Docker remain out of scope entirely.
