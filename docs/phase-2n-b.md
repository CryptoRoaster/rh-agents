# Phase 2N-B — Refreshing a decision basis after a wait

A TradeCase is assembled, PULSE finds no trigger and is rescheduled by policy,
and ninety-five seconds later the trigger is there. The case is complete by every
workflow rule — and it cannot be decided, because readings it would be judged on
were taken before the wait.

Two of them, ageing out of two different contracts:

- **The holder distribution**, carried inside ATLAS's on-chain evidence, ages past
  SENTINEL's own thirty-second source bound. `too_old_for` stops there before
  SENTINEL is asked: `SOURCE_OLDER_THAN_RISK_LIMIT`, detail
  `HOLDERS_OLDER_THAN_RISK_LIMIT`.
- **The execution assessment**, carried in ANCHOR's evidence, has a ninety-second
  life measured from the reference observation it was built on. It expires out of
  the *readiness* contract instead: `RISK_DATA_INCOMPLETE` with the gap
  `ROUTING_AVAILABILITY / STALE / ANCHOR_EXECUTION_EVIDENCE` — and, once the case
  is re-evaluated, `BLOCKED` on `ANCHOR_STALE_EXECUTION_EVIDENCE`.

This phase closes both. It does **not** shorten PULSE's ninety-second interval,
extend SENTINEL's thirty-second bound, or lengthen any evidence or quote life.
All of those are correct as they stand; what was missing was a way to observe a
source again.

## The gap, reproduced

Reproduced through the production composition (`build_stack`) on a controlled
clock: recorded candidates → intake → the real specialists → PULSE waits and is
rescheduled → the clock advances to the next due check → new market observations
are recorded → the next `--once` pass runs. No prepared `READY_FOR_RISK` case, no
directly submitted evidence, no skipping the first wait, and no paid provider or
model call anywhere.

### The holder reading

| Question | Answer |
| --- | --- |
| Which source was observed when? | The market, token metadata and liquidity were recorded again at the recheck. ATLAS's on-chain reading still carried the instant of the first pass, 95 s earlier. |
| Which evidence and which task? | `ONCHAIN_EVIDENCE`, produced by `ATLAS / ASSESS_ONCHAIN_INTEGRITY`, task `SUCCEEDED`. |
| Which check blocked it? | `too_old_for(...)`, applying SENTINEL's own `max_snapshot_age_seconds = 30` before SENTINEL is asked. `SOURCE_OLDER_THAN_RISK_LIMIT`, detail `HOLDERS_OLDER_THAN_RISK_LIMIT`. |
| Had anything final been written? | No request row, no binding, no execution. |

Of the four sources `too_old_for` ages independently, only that one is carried by
evidence: **MARKET**, **TOKEN** and **LIQUIDITY** are read from the recorded
market snapshot (`reference_price(snapshot)`, `base_asset_metadata(snapshot)`,
`snapshot.liquidity`) and were current.

### The execution assessment

The same case, two passes later, with every instant checked against the rows:

| Question | Answer |
| --- | --- |
| Which source was observed when? | ANCHOR assessed against the market observation recorded for its own pass — `observed_at == pass_2 − 5 s` — and quoted a ladder against it. |
| How long was that good for? | `valid_until == observed_at + 90 s`, which is `ANCHOR_EXECUTION_V1.max_reference_age` measured from the reference, never from the run. |
| Which evidence and which task? | `LIQUIDITY_EXECUTION_EVIDENCE`, produced by `ANCHOR / ASSESS_EXECUTION`, task `SUCCEEDED`, one envelope, superseding nothing. |
| Which check blocked it? | The readiness contract: `RISK_DATA_INCOMPLETE`, gap `ROUTING_AVAILABILITY / STALE / ANCHOR_EXECUTION_EVIDENCE`, no blockers. Once anything re-evaluates the case, the workflow reaches the same conclusion its own way: `BLOCKED` / `EXECUTION_EVIDENCE_BLOCKED` / `ANCHOR_STALE_EXECUTION_EVIDENCE`. |
| Had anything final been written? | Again nothing: no request row, no binding, no execution. The setup, the trigger and the case were all still valid. |

Both documented entry points are kept as regressions in
`tests/refresh/test_execution_reproduction.py`: a case that has sat through a
wait, and a case whose refresh run was interrupted by its own budget and finished
its ordered observation one pass later.

## What already existed, and why none of it covered this

Checked before anything was designed.

- **`_rearm_derived_tasks`** re-arms a completed task when one of its *inputs* is
  superseded — and only for tasks with a non-empty `derived_from`, which is FUSE
  alone, and only before the trigger. The policy said so in as many words:
  *"ATLAS looking at a contract again would be a new observation, not a
  re-derivation."* Re-observation was an acknowledged, deliberately unimplemented
  concept.
- **Evidence supersession** (`supersedes_id`, `active_evidence`) already gives one
  live envelope per type and a chain back through what it replaced. It is the
  right mechanism for recording a new reading; it does not decide when one is
  needed. ANCHOR was the one producer that never used it, because until now its
  evidence was written once and never replaced.
- **Evidence validity and SENTINEL's source bound are different horizons**, which
  is exactly why a case could be complete for the workflow and undecidable for
  the risk boundary — and why the two stale sources surface as two different
  refusals.
- **The wait policy, the readiness contract and the risk refusals** already name,
  in typed vocabulary, what is wrong and where it came from. All of that is
  reused unchanged.

## The refresh contract

**One table, in the workflow policy.** `RefreshableSource` names an origin, the
evidence type that carries it, and the labels SENTINEL's own staleness check
reports for it. `TRADE_CASE_V1` declares two:

| Origin | Evidence | Reached by |
| --- | --- | --- |
| `ATLAS_ONCHAIN_EVIDENCE` | `ONCHAIN_EVIDENCE` | SENTINEL's `HOLDERS` staleness label |
| `ANCHOR_EXECUTION_EVIDENCE` | `LIQUIDITY_EXECUTION_EVIDENCE` | a readiness gap, or the case's own blocker |

The role and task type are not written down again: they come from the existing
`requirement(evidence_type)`. `RECORDED_MARKET_OBSERVATION` and
`OPERATOR_CONFIGURED_ASSUMPTION` are deliberately absent and always will be — no
task in this workflow records a market, and a configured assumption was never
observed at all, so declaring either would promise work that cannot be done. A
test holds the declared strings against `RiskFactOrigin` so the two vocabularies
cannot drift apart. (They are strings rather than the enum for one reason: the
risk-data package reads this policy, so importing it back would close a cycle.)

**Only age maps to an observer.** `refreshable_for_gap` answers only for
`RiskDataGapCode.STALE`. Configuration that was never supplied, a fact the
evidence never established, a metric whose coverage could not be proven, an
observation dated in the future — each keeps its own refusal. Reading
`RISK_DATA_INCOMPLETE` as blanket permission to re-observe would turn every one
of those into work.

**One method, on the status owner.** `TradeCaseService.refresh_source(case,
origin)` arms the observing task and returns a typed `SourceRefreshOrder`. It
observes nothing, records no evidence, touches no status and decides nothing.
Five bounds:

- **Only a declared origin** — anything else is `SOURCE_NOT_REFRESHABLE`.
- **Only a case a fresher observation could still move.** `READY_FOR_RISK`, where
  the risk boundary is waiting on preconditions; or `BLOCKED` *on this source
  having expired*, checked here against the envelope's own `effective_status`
  rather than taken from the caller — so a case blocked by a negative assessment
  is `SOURCE_NOT_STALE` and a decided case is `CASE_NOT_READY`. A negative
  re-assessment stays negative; it is never re-armed into a second opinion.
- **Only a slot that finished** — `PENDING` or `RUNNING` is `ALREADY_ORDERED`.
  Two runs racing serialize on the case row, which is what stops a restart or a
  parallel pass from creating duplicate observation work.
- **Only inside the existing claim ceiling** — re-arming spends an attempt of the
  budget the slot has always had.
- **Never a status change** — the evidence set is unchanged until a new reading
  is recorded, so the workflow remains the only thing that moves a case.

**ANCHOR's submission path, completed.** Its context reader now reads its own
live envelope and passes it as `supersedes_evidence_id`, and the handler submits
`supersedes_id` like every other producer. Its idempotency key gains the
generation it replaces (`anchor:{digest}:{superseded}`) for one reason: the
Phase 2K correction anchored `valid_until` to the reference observation so that
re-assessing identical quotes resolves to a replay rather than a conflict, and
without the generation a re-assessment of an unmoved market would resubmit under
the key the first answer already holds while carrying a different supersession.
With it, an unchanged market records an unchanged reading at its own unchanged
instant, and a lost acknowledgement still resolves to one piece of evidence.

**In the run.** `BoundedPaperRun` reads what to ask for from the services' own
output — the refusal's staleness label, its readiness gaps, or a blocked case's
published blockers — mapped through the workflow table. It keeps no idea of its
own about who observes what.

A refresh round orders every source the case still needs *together*, then lets
the owing handlers run in one sweep, so a case needing two observers does not pay
for two passes and the second reading is not taken against a first that has
meanwhile aged. Then it asks again **under the same request key**: the case still
has exactly one canonical request, and this is that question asked once its
preconditions hold.

Each origin is ordered **at most once per case per run**, so the loop is bounded
by the number of declared refreshable sources — two — and a case needing both
cannot turn into a run alternating between two gaps until one of them passes.
Ordering, working and re-asking are ordinary steps against the same step, time
and case budgets. When a new observation does not arrive — the handler refused,
the source had not changed, the budget ran out — the refusal that was already
there is what gets reported, unchanged, and what was ordered stays claimable for
the next explicit pass.

## What the proofs establish

PostgreSQL, real services, production composition. The only fixtures are the
outside edge: recorded market rows written through `MarketRecorder`, the scripted
model, and the chain, holder, origin, social, history and quote sources. No
network call is made or simulated. Every assertion about what happened is read
from rows and attempts, not from summary counters.

| Proof | Test |
| --- | --- |
| PULSE really waits first, and is rescheduled rather than retried | `waited()` in every scenario |
| A holder reading from before the wait blocks, and reading it again does not rejuvenate it | `test_reproduction.py` |
| An execution assessment expires out of readiness, with its exact instants | `test_execution_reproduction.py` |
| A stale assessment plus current sources: a real second ANCHOR attempt, superseding evidence, exactly one PAPER fill | `test_execution_reproduction.py`, `test_execution.py` |
| Holder and execution stale together: both ordered once each, bounded, one fill | `test_both_stale_sources_are_refreshed_once_each_and_the_case_fills` |
| An unchanged quote source leaves the assessment exactly as stale as it was | `test_an_unchanged_quote_source_leaves_the_assessment_stale` |
| A missing route, or a negative re-assessment, produces no fill — and the negative one is not re-armed | `test_a_quote_source_with_no_route_does_not_fill`, `test_a_negative_reassessment_stays_negative` |
| A budget ending between refreshes: nothing partial, and the next explicit run continues the ordered work to one fill | `test_a_budget_that_ends_between_refreshes_is_continued_by_the_next_run`, `test_an_interrupted_refresh_run_is_continued_to_exactly_one_fill` |
| Concurrent runs: one order per source, at most one execution | `test_concurrency.py` (PostgreSQL only) |
| The stored basis names the evidence actually used, and its `RiskContext` still recomputes | `test_the_stored_basis_names_the_sources_the_decision_actually_used`, `test_both_stale_sources_are_refreshed_once_each_and_the_case_fills` |
| A replay reads history and asks no source again | `test_a_replay_of_the_same_request_asks_no_source_again` |
| An approval whose window closed is refused at the fill boundary | `test_an_approval_whose_window_closed_is_refused_at_the_fill` |
| Every refusal of the order itself, and the two vocabularies held together | `test_contract.py` |

## Deliberately not done

- **No third refreshable source.** Only the two origins carried by evidence a task
  can produce again are declared. The market-derived facts come from the
  recorder, and making a market be recorded again is its job, not a run's; a
  configured assumption is not an observation at all.
- **No loosened deadline.** SENTINEL's source bound, PULSE's interval and horizon,
  evidence validity and ANCHOR's quote and reference tolerances are all
  unchanged. The only change to how evidence is written is that ANCHOR now
  supersedes its own previous envelope instead of being unable to replace it.
- **No migration.** Nothing was added to the schema; `alembic check` reports no
  new upgrade operations. Head stays at `0011`.
- No automatic exits or re-entries, no daemon, scheduler or background start, no
  live trading, signing, broadcast or Docker, and no general refactoring beyond
  the helpers the new path shares with the existing one.
