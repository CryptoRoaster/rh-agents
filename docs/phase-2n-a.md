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

**Ports, and what each one costs to compose:**

- `reasoning_provider="fake"` is **not constructible from configuration** and
  never will be: `DeterministicReasoningProvider` replays a script, and a
  configuration has no script to give it. Only `anthropic` composes. This is the
  guarantee that no production configuration can reach a synthetic model.
- **ATLAS** is composed from the request/response `EvmRpcClient` the ATLAS source
  already uses — a block and two contract slots when a handler asks, not the
  websocket runtime and no ingestion. The client is owned by the run and closed
  with it.
- **Market history** for VECTOR is composed from the GeckoTerminal transport and
  network directory, owned by the run and closed with it.
- Both of those are **chain-bound by the contracts they implement**:
  `TokenContractReadPort.chain_snapshot()` takes no chain argument, and the OHLCV
  adapter fixes its chain at construction. With more than one chain enabled there
  is no single correct source to build, so the role reports
  `ONCHAIN_SOURCE_CHAIN_AMBIGUOUS` or `MARKET_HISTORY_CHAIN_AMBIGUOUS` rather
  than serving one chain and silently refusing the others. Making either
  multi-chain means changing the port, which is a deliberate decision this phase
  does not take.
- **FUSE needs no port at all.** It has an evidence requirement in the workflow
  policy — `SYNTHESIZE_EVIDENCE`, optional and not safety-critical — so
  `authorized_task_type` answers for it and its tasks are claimable like any
  other. Its synthesis reads verdicts the specialists already committed, so
  there is no provider and no model beside it.

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
| `PAPER_RUNNER_MAX_CANDIDATES` | `5` | candidates one pass may *process* |
| `PAPER_RUNNER_MAX_NEW_CASES` | `3` | how many of them may become cases |
| `PAPER_RUNNER_MAX_STEPS` | `40` | mutating service calls in one pass |
| `PAPER_RUNNER_MAX_CASES` | `3` | **distinct cases this run works on, across all stages** |
| `PAPER_RUNNER_MAX_SECONDS` | `300` | the whole pass, measured monotonically |
| `PAPER_RUNNER_STEP_TIMEOUT_SECONDS` | `60` | one external wait inside it |

Precisely what each one means, because the differences matter:

**Candidates and new cases are two different quantities**, and each has its own
number. `MAX_CANDIDATES` bounds how many recorded candidates intake *reads and
judges*, passed to `run_cycle(limit=…)`. `MAX_NEW_CASES` bounds how many of them
may become cases, and is applied by lowering the control policy's own per-cycle
ceiling to the smallest of it, that ceiling and the case budget — **before** the
cycle runs, so nothing is opened and then discarded. A case that was never
allowed is never created.

**Cases** counts distinct trade cases the run works on, through intake, worker
steps and the decision path alike. A case counts once however often it is
touched. The working set is **decided before the first claim**: whatever intake
opened, topped up from the cases already alive, oldest first, until the budget
is full. Every claim is narrowed to that set **in the claim query**, so a task
outside the budget is never taken and then dropped — a dropped claim is a lease
nobody is working, held for as long as the lease lasts. A claim whose handler
then times out still spends its case place and its step.

**Steps** counts service calls that may change something: the intake cycle, each
worker attempt that actually claimed a task, each risk request, each fill.
Checked *before* the next such call. An empty claim is not a step; an attempt
that began and was cut off **is** one, because work was started.

**Runtime** is a monotonic deadline, so a clock adjustment can neither extend nor
end a run. It is checked before every mutating step, and every wait — intake,
worker registration, a handler, a database read, the risk request, the fill — is
bounded by `min(step timeout, time remaining)`. The trusted clock is untouched:
it still decides evidence freshness, the approval window and the fill instant,
which are business facts rather than scheduling.

Refused at settings level: `PAPER_RUNNER_ENABLED` without `TRADING_MODE=PAPER`,
and a step timeout longer than the run. Refused before any mutating step: an
enabled role this configuration cannot actually run (`ROLE_NOT_CONFIGURED`),
because proceeding would open cases whose evidence nobody could produce and then
report them as *waiting*, which reads like patience rather than a missing
setting.

Provider budgets are the existing ones; this phase adds no second budget beside
them. A run also needs what every entry already needed and does not default:
`PAPER_REQUESTED_NOTIONAL_USD`, `PAPER_FEE_BPS`, `PAPER_SLIPPAGE_BPS`.

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
role could not run, candidates seen, cases opened, intake refusal codes,
`steps_taken`, `steps_timed_out`, per-case progress, counts of requests, fills
and replays, and technical `errors` as codes.

Three distinctions the output makes deliberately:

- **An empty claim and a timeout are different.** Nothing to do leaves
  `steps_timed_out` at zero; a handler that began and was cut off increments it.
  Reporting them as one would hide a hanging provider behind an empty queue.
- **An unknown outcome is not a failure and not a success.** A decisive call cut
  off mid-flight may have committed or may not have. The case is marked
  `outcome_unknown`; the next explicit run addresses the same order key and finds
  out what really happened. Nothing is invented in either direction.
- **Confirmed work survives a later fault.** The account is accumulated as the
  pass happens rather than assembled at the end. A SENTINEL verdict is written
  into it the moment the risk request returns and before the fill is started, so
  a fill that then fails cannot take a committed approval down with it, and a
  database that stops answering after a fill cannot make the run report zero
  fills for a fill that really happened.
- **An unconfirmed intake is named, not counted.** The cycle commits one case at
  a time, so a cycle that stopped part-way may have opened some. The run does not
  count rows — another run's commits are not its own — it reports
  `intake_outcome_unknown` and leaves the count at zero.

Safe by construction: every value is an identifier this system already exposes, a
count, or a typed reason code from a published vocabulary. No secret, provider
payload or exception text is ever put into the model, so none can be printed.

## Test evidence

37 tests in `tests/runner/`, on a schema carrying markets, workflow and
accounting.

**The whole chain, once, through the production runner.**
`test_the_whole_chain_runs_from_a_recorded_candidate` starts from a recorded
market observation written through the real `MarketRecorder` and runs the real
`build_stack` composition: intake opens the case, the real `WorkerRunner` claims
real tasks, and the real ORBIT, ATLAS, SIGNAL, VECTOR, PULSE and ANCHOR handlers
with their real context readers produce every piece of evidence the workflow
requires. No evidence is submitted by the test and no case is prepared at
`READY_FOR_RISK`. The case then goes through the real risk request,
`src.risk.engine.evaluate`, `CaseFillService`, `PaperExecutor` and the ledger to
a booked fill, with cash and a position to show for it.

**What is substituted, and only at the outside edge:** the model (a provider
that answers from the very prompt payload the handler built, so its citations
name the observation the context really contained), the chain read, the holder
and origin indexers, the social source, the market structure series and the
quote source. Each is a fixture from the suite that owns that boundary, against
the same market. **No provider or model is called for real anywhere**, no
launcher exists, and nothing outlives the call that asked for it.

**Two runs, because that is what the contracts produce.** The first pass carries
the case to a setup; the second, twenty seconds later and after the market has
moved through the level, triggers, assesses execution, and fills. Nothing waits
in between — the pass ends and the task table holds the work.

Also covered: budgets (candidates enforced inside intake, distinct cases across
all stages, steps checked before the next mutating call, a monotonic deadline);
a handler entered exactly once and its cancellation awaited; a real claimable
monitor executed once, rescheduled, and not handled again in the same pass;
confirmed work surviving a later database fault; an unknown outcome reported as
unknown; restart replaying rather than re-ordering; two concurrent runs producing
one order and one fill; a failure inside the fill leaving no partial booking;
every stop; `--once` required; an invalid configuration exiting `2` without
touching anything; a configuration never producing a synthetic model; an enabled
role that cannot be composed refused before any mutating step; and no exit,
re-entry or second case for an executed market.

Full gates: PostgreSQL 3295 passed / 20 skipped, SQLite 3211 passed / 104
skipped, ruff, strict mypy, Alembic at `0011` with `check` clean and offline SQL
generated, frontend typecheck/lint/format/build.

**No migration.** This phase persists nothing of its own.

## Two things CI caught that a local run could not

Both were test-environment faults rather than defects in the runner, and both are
worth recording because a local pass had said otherwise.

**A guard that reads the tracked tree.** `tests/sizing/test_authority.py` pins
where `paper_requested_notional_usd` may be read, so a new file only appears to
it once committed. The guard was extended rather than loosened: the bounded run
is the second legitimate reader, and it now also asserts that the web process
cannot reach the runner at all, which is what keeps "configuring a size enables
nothing" true.

**A fixture that inherited the developer's environment.** `Settings` consults the
ambient environment for anything a test leaves unset, so an `ANTHROPIC_API_KEY`
exported in one shell made the end-to-end fixtures pass there and fail
everywhere else. The runner test settings now state the value themselves. It is
never used — every test that selects a provider supplies the port itself — and
exists only because a selected provider must be fully configured.

## The guard the first CI run caught

`tests/sizing/test_authority.py` pins where `paper_requested_notional_usd` may
be read, and it reads the tracked tree — so a new file only appears to it once
committed, which is exactly when CI saw it and the local run had not.

The guard was extended rather than loosened. The bounded run is the second
legitimate reader: the risk request has always needed a configured amount, and
until now only a test could supply one. What the guard now also asserts is the
thing that keeps "configuring a size enables nothing" true — **the web process
cannot reach the runner at all**, so an amount sitting in the environment can
begin nothing by being present.

## A budget rule this document previously claimed and the code did not have

An earlier revision of these notes described the intake policy as being lowered
before the cycle ran. `build_stack` never passed that policy, so the ceiling in
force was the control plane's own five and the runner's candidate number bounded
nothing — a report can describe a rule the code does not implement, and this one
did. Reproduced by counting `trade_cases` rows rather than reading the summary:
three cases written with a budget of one.

Two further gaps in the same area, found the same way: worker claims never
entered the run's working set, so the claim scope stayed unset whenever intake
had not already filled the budget; and FUSE was reported `ROLE_NOT_CLAIMABLE` on
the strength of a stale comment in the runtime rather than the workflow policy,
which has had an evidence requirement for it all along.

All three are fixed above and proved against the database in
`tests/runner/test_budgets.py` and `tests/runner/test_end_to_end.py`.

## A limit the end-to-end proof exposed

PULSE re-checks a pending trigger on a **ninety-second** interval by workflow
policy. SENTINEL refuses any source older than **thirty seconds**. So a trigger
found on a *rescheduled* check arrives with on-chain and sentiment evidence the
risk engine has already stopped accepting, and the run correctly reports
`SOURCE_OLDER_THAN_RISK_LIMIT` rather than filling.

This is an interaction between two existing policies, not a defect in either and
not something this phase loosened: the monitor's interval is deliberate, and so
is the freshness bound. A deployment that wants a trigger to reach a fill needs
either evidence that is refreshed closer to the decision, or the two windows
reconciled. Both are contract changes, and neither belongs in a runner.

The end-to-end test therefore has the monitor make its **first** check after the
market moved, which is inside both windows. That is a real sequence rather than
a workaround, and the limit is recorded here instead of being hidden by it.

## Remaining limits

- **A run promises nothing.** Waiting and blocked cases are ordinary outcomes.
- **ATLAS and VECTOR history are chain-bound**, so a run with more than one chain
  enabled reports them unavailable rather than serving one chain silently.
  Making either multi-chain means changing the port.
- **Only `anthropic` composes as a reasoning provider**, by design.
- **A rescheduled PULSE check cannot reach a fill** while its interval exceeds
  SENTINEL's source bound; see above.
- **No automatic exit or re-entry**, and no automatic stop-loss or take-profit.
- **No daemon, scheduler or background start.** One pass per invocation.
- **No public write endpoint.** The API remains read-only.
- Live execution, signing, broadcast and Docker remain out of scope entirely.
