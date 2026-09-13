# Phase 2I: PULSE — the deterministic trigger monitor

PULSE answers one question: **has the currently authoritative VECTOR trigger
condition become true, before its setup expires?**

It answers with arithmetic. There is no model, no prompt, no reasoning provider
and no paid call anywhere in the package, and a test reads the import graph to
keep it that way. A comparison between two Decimals has one correct answer; a
probabilistic one would add cost, latency and variance to it, and would make it
impossible to say afterwards why the system acted.

It does not decide whether a trade is good, how large it should be, which route
to take, what slippage to accept, whether liquidity is sufficient or whether risk
is acceptable. Those are ANCHOR's and SENTINEL's questions. PULSE produces
`TRIGGER_EVIDENCE` and nothing else, and only when the condition actually held.

## What the audit established first

| Fact | Value |
| --- | --- |
| Role / task | `PULSE` / `WAIT_FOR_TRIGGER` |
| Evidence type | `TRIGGER` |
| Required | yes |
| Safety-critical | **yes** — so `TRIGGER` is in `safety_types` and participates in `risk_input_digest` |
| `before_trigger` | no — this is the gate the pre-trigger prerequisites lead to |
| Transition | `READY_FOR_TRIGGER` → `TRIGGERED` → settles at `EXECUTION_EVIDENCE_PENDING` |
| Setup binding | `TriggerPayload.setup_evidence_id`, revalidated server-side |
| Supersession | a trigger naming a superseded setup is refused at submission |

The evaluator already knew how to reject a trigger that does not match the
current setup (`TRIGGER_DOES_NOT_MATCH_CURRENT_SETUP`), and the worker runtime
already refused to *record* one (`_require_current_references`). Phase 2I adds
the thing that produces them, not the rules that govern them.

## The one architectural gap this phase had to close

Every specialist before PULSE computes an answer once. A monitor does not: its
normal state is "not yet", possibly for hours. The Phase 2B runtime had two
answers — success and failure — so waiting could only have been expressed as a
failure, which would have

* spent a three-attempt retry budget within a couple of minutes,
* recorded ordinary operation as a run of incidents in immutable attempt
  history, and
* made a genuinely broken monitor indistinguishable from a patient one.

So the runtime gained a third outcome. `TaskAttemptOutcome.WAITING` records an
attempt that did its work and found the world not yet ready; it carries no
failure category, and the type system enforces that. `report_task_wait` closes
the attempt, releases the lease, returns the task to `PENDING` and sets
`next_eligible_at` — the retry path's mechanics without the retry's meaning.

**The loop lives in the database.** One claim performs exactly one check. There
is no sleeping inside a lease and no polling loop in the worker process, so a
thousand waiting cases cost a thousand rows rather than a thousand coroutines.

**A worker proposes, the runtime decides.** The worker suggests a recheck
interval and `WorkerRuntimePolicy.wait_interval` clamps it between ten seconds
and fifteen minutes. Without a floor a worker could poll a provider as fast as it
liked; without a ceiling a watch could go to sleep for hours without saying so.

**A watch is bounded.** `TaskDefinition.max_attempts` becomes a *watch* budget
rather than a retry budget: 300 claims at PULSE's ninety-second cadence covers
seven and a half hours, comfortably beyond the four a VECTOR setup may live. In
practice the setup's own expiry ends the watch first. Running out produces
`TASK_WATCH_EXHAUSTED` — a system that would watch forever has no way to say it
has stopped.

The outcome column is `String(40)` with no value whitelist, so none of this
needed a migration. Alembic head remains `0006`.

## The comparison

Only the grammar VECTOR already wrote. No prose triggers, and nothing invented:

| Condition | Fires when | Boundary |
| --- | --- | --- |
| `PRICE_GTE X` | `price >= X` | inclusive |
| `PRICE_LTE X` | `price <= X` | inclusive |
| `PRICE_IN_RANGE lo..hi` | `lo <= price <= hi` | inclusive both ends |

Decimal throughout; no float ever participates and there is no epsilon. Both
thresholds are inclusive because the grammar names them GTE and LTE — a price
exactly at the level has reached it, and an exclusive comparison would silently
mean something the setup did not say. Trailing zeros do not move a boundary, and
prices from `0.000000000123` to `64000.50` compare exactly.

**No smoothing, no confirmation, no debounce.** The first fresh, valid,
authoritative observation satisfying the condition triggers it. A moving average,
an epsilon or a "two consecutive ticks" rule would each be a trading policy
wearing the costume of a safety measure, and none is in the contract VECTOR
wrote. If confirmation is ever wanted it belongs in a separately versioned policy
that says so out loud.

## What is checked, and in what order

Order is deliberate. Identity and units come first, because a price from another
market or in another unit is not a price that has failed to cross a threshold —
it is a number that cannot be compared to this condition at all, and reporting it
as "not yet" would schedule a patient wait for an answer that can never arrive.

| Check | Outcome | Treated as |
| --- | --- | --- |
| no current setup | `NO_CURRENT_SETUP` | wait — one may yet arrive |
| `now >= expires_at` | `SETUP_EXPIRED` | wait — the watch is over |
| `now < valid_from` | `NOT_TRIGGERED` | wait |
| no usable price | `OBSERVATION_STALE` | wait |
| another pool | `OBSERVATION_INVALID` | **fault** |
| another price unit | `OBSERVATION_INVALID` | **fault** |
| future-dated beyond skew | `OBSERVATION_INVALID` | **fault** |
| observation predates the setup | `OBSERVATION_PRECEDES_SETUP` | wait |
| observation after the window | `OBSERVATION_INVALID` | **fault** |
| older than the freshness window | `OBSERVATION_STALE` | wait |
| condition not met | `NOT_TRIGGERED` | wait |
| condition met | `TRIGGERED` | evidence |

One of these was reclassified during implementation. An observation *predating*
the setup began as a fault, and an integration test showed why that was wrong:
immediately after VECTOR publishes, the market layer's newest recorded snapshot
is always older than the proposal. Treating that as a fault would have failed the
task on every fresh setup. It is a wait, and it resolves itself on the next
recorded observation.

## Time

All freshness is measured from the market's **own** observation time. A stale
price fetched a second ago is still stale; fetch time, worker start time and
database read time reach nothing.

| Bound | Value | Why |
| --- | --- | --- |
| `max_observation_age` | 2 minutes | a trigger asserts the market is at a level *now* |
| `poll_interval` | 90 seconds | matches the market watcher; faster re-reads the same number |
| `max_clock_skew` | 5 seconds | two clocks at an instant boundary may differ |

VECTOR's five-minute window is deliberately **not** copied. A setup generator
reasons about where levels are and tolerates a slightly older picture; a monitor
asserts that a level has just been reached. The policy refuses to exist if skew
reaches the freshness window (the two rules would disagree about one timestamp)
or if polling is slower than staleness (most checks would see data they must
refuse).

**Expiry is exclusive.** A setup expiring at 10:00 is not valid *at* 10:00.
Pinned by test at the microsecond either side, because leaving it ambiguous would
put a trade's authority in a rounding question.

## Provider load

PULSE reads **recorded** market observations through the existing market layer.
It issues no provider request of its own. The market watcher already records
snapshots on a ninety-second cadence and the upstream public API caches for a
minute, so a monitor that fetched on every check would become the highest-volume
consumer of a rate-limited API in order to re-read a number that could not have
changed. Freshness is enforced against source time, so consuming recorded data
costs nothing in correctness.

No event bus was introduced. Bounded scheduled polling over durable task state
was the smaller change and is what the runtime already supports.

## Evidence

Written **only** on a positive trigger. A check that found nothing writes nothing
at all — waiting belongs to task and attempt history, not to the evidence table,
and one row per unmet poll would produce a large useless history.

`TriggerDetail` records the comparison that was made: the condition and its
levels, the observed price, the market's own observation time, the observation
and snapshot identities, the pool, the provider, the price basis, the setup
identity and fingerprint, and the policy version. No prose, no confidence, no
score, no sentiment — a comparison between two Decimals has none of those, and
the shared envelope `confidence` field is left empty rather than filled with an
invented number.

The evidence's `valid_until` is the setup's expiry, so a trigger stops being
current exactly when the setup it fired for does.

**Idempotency is by event, not by attempt.** The digest covers the case, the
setup identity, the condition, the market, the price, its unit and the source
observation time — and excludes the lease, the worker, the attempt number and the
moment of evaluation. The same crossing noticed by a different worker on a
different attempt is the same event, so a replayed acknowledgement resolves to
one trigger rather than two.

## Races

| Race | Behaviour |
| --- | --- |
| VECTOR publishes a new setup mid-check | submission refused with `TASK_SUPERSEDED`; no trigger for the old setup exists |
| A scheduled check wakes after a new setup | it watches the new condition, because selection goes through `active_evidence` |
| Lease expires before submission | refused with `LEASE_EXPIRED` — and a *wait* is fenced identically |
| Acknowledgement lost, result replayed | one trigger, one attempt |
| A later price also satisfies a fired setup | the task is terminal; no second authoritative trigger |
| Many cases waiting | each claims and reschedules independently, with no cross-case trigger |

A trigger is a historical fact about a moment. The runtime revalidates that the
setup is still current, but does not require that the evaluated price is still
the latest price: the crossing happened when it happened.

## Handoff

A positive trigger moves the case through `TRIGGERED` and it settles at
`EXECUTION_EVIDENCE_PENDING`, waiting for ANCHOR. PULSE does not call ANCHOR,
does not call SENTINEL, creates no risk binding and cannot force a TradeCase
status. Specialist workers are not coupled to each other; the workflow decides
what happens next.

## Security

* `PulseCapabilities` is exactly `{lease, context, submit}`.
* `PulseContextPort` has one method, `trigger_context`.
* The package imports no reasoning provider, no `httpx`, no `sqlalchemy`, no RPC
  client and no provider adapter — checked against the import graph rather than
  the source text, because the docstrings name the very things they forbid.
* No wallet, signer, executor, Ledger writer, SENTINEL mutation or RiskBinding
  creation; no `TradeCaseStatus` anywhere in the package.
* No public mutation API: the routes remain GET-only and there is no
  force-trigger endpoint of any kind.
* `pulse_worker_enabled` defaults to `False`, and no startup path references the
  handler, the reader or the flag.
* No Docker, no containers, no signing, no broadcast, no live execution.

## Compatibility

`TriggerDetail` is additive and optional, so Phase 2A `TriggerPayload` values
parse and replay unchanged. `TaskDefinition.max_attempts` is optional and absent
for every other role. No migration; Alembic head remains `0006`.
