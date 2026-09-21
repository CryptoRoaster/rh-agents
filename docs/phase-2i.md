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

**The schedule is the server's, not the worker's.** An audit found the first
version of this too permissive: a worker named its own recheck interval and the
runtime merely clamped it, and *any* role could call `report_task_wait` with any
reason — ATLAS could have postponed its own task by fifteen minutes at a time,
indefinitely. Cadence is policy.

`TaskDefinition.wait` now carries a server-owned `WaitPolicy`: the interval, the
horizon, the reasons that may be reported, and which of those end a watch rather
than continue it. A task without one may not wait at all, which is every other
role — they compute an answer once and have nothing to be patient about. An
unknown reason is refused rather than recorded, so a worker cannot invent a
category to wait under.

The one thing a worker may still say about timing is `not_after`, and it can only
ever **shorten** a wait. A monitor knows when the thing it watches stops being
watchable, and scheduling a check past that point would queue work that cannot
succeed. There is no way to express postponement, which is the direction that
would matter.

**A watch is bounded, and the bound is derived.** The horizon is expressed as a
duration — five hours — and the permitted number of checks follows from it
(`horizon / interval`), rather than being a number somebody picked. A test holds
the horizon against `VECTOR_SETUP_V1.max_setup_lifetime`, so if the longest
permitted setup ever changes, the relationship fails loudly instead of leaving a
watch that stops early. Running out produces `TASK_WATCH_EXHAUSTED`; reaching a
terminal reason produces `TASK_WATCH_ENDED`. Neither ever masquerades as a
provider failure, a risk rejection or a setup invalidation.

**Waiting and failing have separate budgets.** They were sharing one counter,
which meant two hundred ordinary rechecks would have consumed the allowance meant
for things going wrong, and a handful of provider outages could have consumed the
watch. Both counts are now read from immutable attempt history — waits against
the watch horizon, failures against `max_attempts` — so neither can spend the
other. For every one-shot role the failure count is identical to what the claim
counter previously said, so nothing about their behaviour changed.

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

### A price the comparison cannot represent

The market layer records whatever a provider reports — its `Amount` permits a
hundred significant digits — while everything downstream of a trigger holds money
in the ledger's `Numeric(38, 18)`, which is the envelope `PriceObservation`
declares. A real BNB Smart Chain pool reported a price with nineteen decimal
places, and the two contracts disagreed about it.

**The recorded observation is unaffected.** It was stored, it stays stored, and
it is still what the market layer says about that pool. What changes is only what
PULSE can build from it: `price_observation()` returns `None`, so the reading
does not appear in the window and `latest` is `None` for that check. That is the
same absence as an unavailable price, reported through the same wait, and it is
deliberately not a rounded number — quantizing would change what the market said
in the one place where a comparison decides whether an order is armed, and would
arm on a figure the ledger could not then store.

Only the envelope refusal is read this way (`decimal_max_places`,
`decimal_max_digits`, `decimal_whole_digits` on the `price` field). Every other
validation failure is a wiring fault and still raises: a market answering about
the wrong pair must not turn into a monitor quietly seeing no price. A price
outside the envelope therefore remains unusable for PULSE; this widens no
supported precision.

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
| window truncated, nothing seen crossed | `OBSERVATION_BUDGET_EXCEEDED` | **fault** |
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
| `max_observations` | 64 | one check's window; far above the ~2 the cadence implies |

VECTOR's five-minute window is deliberately **not** copied. A setup generator
reasons about where levels are and tolerates a slightly older picture; a monitor
asserts that a level has just been reached. The policy refuses to exist if skew
reaches the freshness window (the two rules would disagree about one timestamp)
or if polling is slower than staleness (most checks would see data they must
refuse).

**Expiry is exclusive.** A setup expiring at 10:00 is not valid *at* 10:00.
Pinned by test at the microsecond either side, because leaving it ambiguous would
put a trade's authority in a rounding question.

## Observation coverage: what a check actually looks at

A follow-up audit found the most serious defect of this phase. PULSE read the
**latest** recorded observation and nothing else, so a price that crossed the
level and came back before the next check was invisible:

```
recorded:  T-80s  1.18      trigger: PRICE_GTE 1.20
           T-50s  1.22      check at T
           T-10s  1.17
```

The system had durably recorded 1.22 during the setup's valid window and would
have reported that nothing happened. Worse, no read on the market layer could
reach it: `MarketReader.markets` ranks rows per stream and keeps only the newest,
which is the right answer for "what is this market doing now" and the wrong one
for "did this ever happen".

So the market layer gained `MarketReader.observations`: every recorded
observation for one market inside a closed window, ordered oldest first by the
market's **own** observation time, capped, with `recorded_at` and `id` breaking
ties only. A row inserted late never becomes recent, because insertion time
cannot reorder the window.

Each check now reads the window

```
since = max(setup.valid_from, now - max_observation_age)
until = now + max_clock_skew
```

and scans it oldest first. The **first** qualifying observation is the event, so
the evidence answers "when did this system first observe the trigger?" with a
stable fact rather than with whichever row happened to be newest when somebody
looked. The same applies to the range condition: a band entered and left is still
a band that was entered.

### What PULSE promises, exactly

It does not watch the market. It watches what the market layer **recorded**:

```
DEX reality → market watcher → recorded observations → PULSE
```

Within that stream it will not skip a qualifying observation merely because a
newer non-qualifying one exists. Outside it, it promises nothing at all.
Detection latency is bounded by the observation cadence plus the task scheduling
cadence plus runtime delay — on the order of minutes, not ticks. That is
acceptable for PAPER and is not claimed to be anything else.

### Freshness is a separate question from coverage

A crossing older than `max_observation_age` is still ignored, deliberately. It
is a fact about a market that has since moved on, and acting on it now would be
acting on a price nobody can still see. A crossing *inside* the window is never
skipped. The two rules do different jobs and both hold.

This makes explicit something worth stating plainly: **a recorded crossing is not
a standing execution opportunity.** PULSE proves that a configured trigger was
observed. It does not prove the price is still there. ANCHOR evaluates current
execution conditions afterwards, and may legitimately refuse a trade whose
trigger genuinely fired.

### A truncated window is never a confident negative

The read returns one row beyond its limit so truncation is knowable. If more
observations existed than the bounded read may return and none of the visible
ones crossed, the check reports `OBSERVATION_BUDGET_EXCEEDED` rather than "not
yet" — a negative would be a claim about rows nobody looked at. If a crossing
*was* found among the visible ones it still triggers, because finding one is
positive evidence regardless of what else was missed.

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
parse and replay unchanged. `TaskDefinition.wait` is optional and absent for
every other role, and `MarketReader.observations` is a new read rather than a
change to an existing one. No migration; Alembic head remains `0006`.
