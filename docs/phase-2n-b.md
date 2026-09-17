# Phase 2N-B — Refreshing a decision basis after a wait

A TradeCase is assembled, PULSE finds no trigger and is rescheduled by policy,
and ninety-five seconds later the trigger is there. The case is complete by every
workflow rule — and the risk request is refused, because one of the sources it
would be judged on was observed before the wait began.

This phase closes that. It does **not** shorten PULSE's ninety-second interval
and does **not** extend SENTINEL's thirty-second bound. Both are correct as they
stand; what was missing was a way to observe a source again.

## The gap, reproduced

Reproduced through the production composition (`build_stack`) on a controlled
clock: recorded candidates → intake → the real specialists → PULSE waits and is
rescheduled → the clock advances to the next due check → new market observations
are recorded → a second `--once` pass runs. No prepared `READY_FOR_RISK` case, no
directly submitted evidence, no skipping the first wait, and no paid provider or
model call anywhere.

What the second pass found, before anything in this phase existed:

| Question | Answer |
| --- | --- |
| Which source was observed when? | The market, token metadata and liquidity were recorded again at the recheck. ATLAS's on-chain reading still carried the instant of the first pass, 95 s earlier. |
| Which evidence and which task? | `ONCHAIN_EVIDENCE`, produced by `ATLAS / ASSESS_ONCHAIN_INTEGRITY`, task `SUCCEEDED` at attempt 1. |
| Which check blocked it? | `too_old_for(...)` in `RiskRequestService`, applying SENTINEL's own `max_snapshot_age_seconds = 30` **before** SENTINEL is asked. Refusal `SOURCE_OLDER_THAN_RISK_LIMIT`, detail `HOLDERS_OLDER_THAN_RISK_LIMIT`. |
| Had anything final been written? | No. No `trade_case_risk_requests` row, no binding, no execution. The case's one canonical request was still unspent. |

Exactly one of the four sources SENTINEL ages independently was stale, and the
distinction matters:

- **MARKET, TOKEN, LIQUIDITY** come from the recorded market snapshot —
  `reference_price(snapshot)`, `base_asset_metadata(snapshot)` and
  `snapshot.liquidity`. They were current, because the market had been recorded
  again.
- **HOLDERS** comes from `payload.intelligence.holders.observed_at` inside
  ATLAS's on-chain evidence. Nothing had observed the chain since the case was
  assembled.

`tests/refresh/test_reproduction.py` keeps this reproducible in its durable form:
the second pass leaves the chain-side fixtures at the first instant, so the world
genuinely has not been observed again, and the case still cannot be decided.

## What already existed, and why none of it covered this

Checked before anything was designed.

- **`_rearm_derived_tasks`** re-arms a completed task when one of its *inputs* is
  superseded — and only for tasks with a non-empty `derived_from`, which is FUSE
  alone, and only before the trigger. The policy says why in as many words:
  *"ATLAS looking at a contract again would be a new observation, not a
  re-derivation."* Re-observation was an acknowledged, deliberately unimplemented
  concept, not an oversight to be papered over.
- **Evidence supersession** (`supersedes_id`, `active_evidence`) already gives one
  live envelope per type and a chain back through what it replaced. It is the
  right mechanism for recording a new reading; it does not decide when one is
  needed.
- **Evidence validity** (`valid_until`, roughly ten minutes for ATLAS) is a
  different and longer horizon than SENTINEL's thirty-second source bound. The
  workflow therefore considered the case complete while the risk engine refused
  it — which is the gap stated precisely.
- **The wait policy** already reschedules PULSE durably, and the risk request
  already refuses in a typed vocabulary that *names the stale source*. Both were
  reused unchanged.

Nothing existed that could ask a specialist to observe its source again.

## The refresh contract

**One declaration, in the workflow policy.** `RefreshableSource` names a risk
source and the evidence type that carries it; `TRADE_CASE_V1` declares exactly
one, `HOLDERS → ONCHAIN_EVIDENCE`. The task to arm is then found through the
existing `requirement(evidence_type)`, so the role and task type are not written
down a second time. The three sources that come from the recorded market are
deliberately absent: no task in this workflow observes a market, and a run that
pretended otherwise would be inventing a source.

**One method, on the status owner.** `TradeCaseService.refresh_source(case,
source)` arms the observing task and returns a typed `SourceRefreshOrder`. It
observes nothing, records no evidence, touches no status and decides nothing
about the case. What the handler then finds travels the ordinary submission path,
supersedes the reading it replaces, and is re-evaluated like any other evidence.

Five bounds, each doing real work:

- **Only a declared source** — anything else is `SOURCE_NOT_REFRESHABLE`.
- **Only a case waiting on a risk request** — `READY_FOR_RISK` and nothing else.
  Earlier the ordinary tasks are running anyway; later the decision has been
  taken, and remaking its inputs would be reopening a settled question.
- **Only a slot that finished** — a `PENDING` or `RUNNING` task already
  represents outstanding work, so a second order is `ALREADY_ORDERED`. Two runs
  racing here serialize on the case row, which is what stops a restart or a
  parallel pass from creating duplicate observation work.
- **Only inside the slot's existing claim ceiling** — re-arming spends an attempt
  of the budget the task has always had. No new budget was invented.
- **Never a status change** — `_stabilize` derives status from evidence, and the
  evidence set is unchanged until a new reading is recorded. The workflow remains
  the only thing that moves a case.

Re-arming itself is now one helper, `_rearm`, shared with `_rearm_derived_tasks`,
so every reason produces the same durable shape: the slot's own row, `attempt`
bumped, lease fields cleared, and an event naming the task, the role, the new
attempt and the reason.

**In the run.** `BoundedPaperRun._refresh` acts on one refusal only —
`SOURCE_OLDER_THAN_RISK_LIMIT` — reads the source name out of the detail via
`stale_source`, orders one new observation, lets the handler run through the same
claim loop the rest of the pass uses, and then asks **the same question under the
same request key**. The case still has exactly one canonical request; this is
that request asked once its preconditions are in place, not a second request
invented for a second chance.

Bounded by: **one refresh per case per run** (a second would be retry-until-pass);
the same step and time budgets as everything else, checked before each call; the
workflow's own refusal when the slot is armed, spent or expired; and — when the
new observation does not arrive — the original refusal, reported unchanged. There
is no polling, no sleep, no busy-loop and no retry in this process.

## What the proofs establish

PostgreSQL, real services, production composition. The only fixtures are the
outside edge: recorded market rows written through `MarketRecorder`, the scripted
model, and the chain, holder, origin, social, history and quote sources. No
network call is made or simulated.

| Proof | Test |
| --- | --- |
| PULSE really waits first, and is rescheduled rather than retried | `waited()` in every scenario; `test_reproduction.py` |
| New observations plus a real re-assessment produce exactly one PAPER fill | `test_new_observations_and_a_reassessment_make_exactly_one_fill` |
| An unchanged source stays exactly as old after being read again | `test_reproduction.py`, `test_a_run_refreshes_one_case_at_most_once` |
| No observer, or a re-assessment that refuses — no fill | `test_without_an_observer_the_refusal_stands_and_the_order_survives`, `test_a_reassessment_that_refuses_does_not_fill` |
| A replaced source invalidates the basis computed from it | `test_the_stored_basis_names_the_sources_the_decision_actually_used` |
| An approval whose window closed is refused at the fill boundary | `test_an_approval_whose_window_closed_is_refused_at_the_fill` |
| A budget that ends mid-refresh stops, books nothing, and leaves the order claimable | `test_a_budget_that_runs_out_mid_refresh_stops_and_books_nothing` |
| Two concurrent runs order once and execute once | `test_concurrency.py` (PostgreSQL only) |
| The stored basis names the envelope actually used | `test_the_stored_basis_names_the_sources_the_decision_actually_used` |
| A replay reads history and asks no source again | `test_a_replay_of_the_same_request_asks_no_source_again` |
| Every refusal of the order itself | `test_contract.py` |

The last one is worth spelling out: `test_contract.py` drives
`TradeCaseService.refresh_source` directly against cases the production stack
really built, and covers `SOURCE_NOT_REFRESHABLE`, `CASE_NOT_READY` both before
the trigger and after the decision, `ALREADY_ORDERED`, the concurrency-conflict
guard on `expected_revision`, and the timeline event that makes an order
traceable to its source and case revision.

## Deliberately not done

- **No second refreshable source.** Only `HOLDERS` is declared, because only
  `HOLDERS` is carried by evidence a task can produce again. The other three come
  from the recorded market, and making a market be recorded again is the
  recorder's job, not a run's.
- **A named second gap, left open.** Over several passes the same scenario
  eventually refuses with `RISK_DATA_INCOMPLETE` and the data gap
  `ROUTING_AVAILABILITY / STALE / ANCHOR_EXECUTION_EVIDENCE`: ANCHOR's execution
  evidence ages out of the readiness check the way the holder reading ages out of
  the staleness check, and nothing re-arms ANCHOR either. That is a *different*
  check with a different refusal, it was not what the reproduction showed, and
  closing it is a separate decision about how long an executable route stays
  believable. It is recorded here rather than folded in quietly.
- **No migration.** Nothing was added to the schema; `alembic check` reports no
  new upgrade operations. Head stays at `0011`.
- No automatic exit or re-entry, no daemon, scheduler or background start, no
  live trading, signing, broadcast or Docker, and no general refactoring beyond
  extracting the two helpers the new path shares with the existing one.
