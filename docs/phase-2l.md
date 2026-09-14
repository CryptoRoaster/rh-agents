# Phase 2L: COMMANDER — deterministic orchestration control plane

COMMANDER answers one operational question about one TradeCase: **what safe
workflow action, if any, is currently eligible?**

It is the component most likely to acquire authority by accident. It touches
every stage, so every capability looks locally reasonable — a task setter to
unstick a stalled case, a status write to record what is obviously true, a trade
size to get the demo moving. Each would be defensible on its own, and together
they would be a second system with none of the first one's constraints.

So most of this document is about what it does not do.

---

## What the repository actually said

The design follows repository truth rather than the shape an orchestrator is
usually assumed to have. The audit found five things that decided the phase:

| Question | What the code said |
|---|---|
| COMMANDER task | `TaskDefinition(COMMANDER, "OPEN_TRADE_CASE", required=True, completed_on_open=True)` — a record that the case opened, marked `SUCCEEDED` immediately |
| Claimable? | **No.** `authorized_task_type(COMMANDER)` returns `None`, because it derives from evidence requirements and COMMANDER has none |
| Could it ever succeed at a task? | **No.** The runtime's only success outcome is an evidence submission, and `authorized_evidence_type(COMMANDER)` is `None`. A role that cannot submit evidence can only wait or fail |
| Case intake | `open_trade_case` is called **only from tests**. Every API route is a `GET`. An autonomous system had no way to begin |
| Risk invocation | `record_risk_decision` accepts an **already-computed** decision. Nothing in the workflow computes one |
| Trade size | **Nothing in `src/` constructs a `TradeIntent`** — and `risk.engine.evaluate` requires one, because a `TradeIntent` carries the quantity |

The last two together are the central finding of this phase, and they are
covered in their own section below.

Two further facts shaped the boundaries: `PaperTradingService` is a Phase 0
pipeline with **no TradeCase connection** at all (`source="COMMANDER"` in it is
a string label on an order, not a call into this), and every specialist task is
already created when a case opens — so there is no task scheduling for a
coordinator to do.

---

## The sizing gap

To ask SENTINEL about a TradeCase, something must build a `TradeIntent`, and a
`TradeIntent` carries a quantity. Nothing produces one.

Three numbers exist that are the right *shape* — a positive amount, in dollars,
already validated, sitting exactly where a size would go:

| Number | What it means | Why it is not a size |
|---|---|---|
| VECTOR's proposal | — | The schema **forbids** size, notional and portfolio fraction, with `extra="forbid"`, so proposing one is a parse error rather than a field somebody downstream might read |
| `largest_tested_acceptable_notional_usd` | ANCHOR tested the market at this size and it held | A fact about liquidity. `AT_LEAST 50,000` means the ladder never found the ceiling, not that fifty thousand is the trade |
| `max_additional_notional_usd` | SENTINEL's ceiling | A limit. "You may not exceed this" is not "trade this" |

Using either of the last two would turn a coordinator into a sizing strategy —
and an unusually bad one, since both are maxima.

So the control plane stops. At `READY_FOR_RISK` it reports
`AUTONOMOUS_SIZING_INPUT_MISSING`, which is a typed architectural gap rather
than a failure. The end-to-end test drives a real case through every specialist
stage and asserts exactly that stop.

**This is the blocker for autonomous PAPER execution.** Phase 2M has to decide
where a requested notional comes from — and that decision is a strategy
decision, which is why it does not belong in a coordinator.

---

## It is not a second workflow engine

The Phase 2A evaluator owns every status, requirement, blocker and freshness
rule. COMMANDER reads that verdict; it does not recompute it. Two authorities on
one question only have to disagree once, and the cheapest way for them to
disagree is for one of them to be updated.

So the decision is a lookup from the status the evaluator published:

| Status | Disposition | Who owns the next move |
|---|---|---|
| `EVIDENCE_PENDING` | `AWAIT_SPECIALISTS` | the specialists |
| `READY_FOR_TRIGGER` | `AWAIT_TRIGGER` | PULSE — COMMANDER never reads a price |
| `TRIGGERED` | `AWAIT_EXECUTION_EVIDENCE` | ANCHOR — COMMANDER never quotes |
| `BLOCKED` | `HALTED` | nobody; a blocker is not a thing coordination fixes |
| `READY_FOR_RISK` | `BLOCKED_ON_MISSING_CAPABILITY` | the sizing gap above |
| terminal | `HALTED` | nobody |

Ordering is authority: a system stop is checked before any case-level
eligibility, and terminal states before any waiting state, so no later branch
can reach past an earlier one.

---

## It never writes

There is no status setter, no forced transition, no task administration and no
risk construction. The placeholder capability was narrowed in this phase: it
carried `create_required_tasks` and `evaluate_trade_case`, the first dead weight
because tasks are created at case open, the second a way for a worker to trigger
an authoritative recomputation on its own schedule.

`CommanderCapabilities` is now `{ lease, context }` — read only. The one
authoritative write in the whole package is opening a case, through the existing
service, with a derived identity.

A test asserts the case revision is unchanged after repeated observation.
Revision counts writes.

---

## Autonomous case intake

This is the one real gap the phase closes. Before it, a TradeCase could only be
opened by test code.

Intake decides whether the **machinery** may open a case — supported chain,
valid canonical identity, fresh observation, no active duplicate, bounded per
cycle — and never which candidate looks better. There is no ranking by
liquidity, no momentum filter, no hype threshold and no score. ORBIT exists to
judge whether a candidate is worth pursuing, and a coordinator that pre-filtered
on market grounds would be a second analyst whose reasoning nobody recorded.

Candidates are taken oldest-observation-first: an order, deliberately not a
ranking.

### Idempotency is derived, not checked

The duplicate check is advisory — it produces a clear refusal reason and saves
work, and two racing workers will both pass it. Safety comes from the case
identity being derived from the candidate, so the second insert collides with a
unique constraint and resolves to the first case.

That only works if **every input to the open fingerprint is a function of the
candidate**. The first implementation got this wrong: each intake service
generated its own correlation UUID and computed `expires_at` from its own clock,
so two workers racing on one candidate produced two different fingerprints and
collided as `IDEMPOTENCY_CONFLICT` instead of converging. Both are now derived —
the correlation from the intake key, the expiry from the candidate's own
observation time.

Idempotency that only holds when one process runs is not idempotency, and the
PostgreSQL concurrency test is what caught it.

---

## FUSE stays advisory

A control plane is the natural place to break this. It is the first component
that sees everything at once, so it is the first that could quietly let an
advisory reading gate a decision.

Three things keep it inert:

* The synthesis is **excluded from the canonical evidence list** and travels as
  its own `advisory` field instead. Were it in the evidence list it would enter
  the context digest, and recording a synthesis would then invalidate any
  in-flight coordination decision — real power over the control plane, granted
  to the one component explicitly authoritative over nothing.
* The **FUSE task is excluded from the digest** for the same reason: only
  required tasks are hashed, because an optional advisory task finishing cannot
  change what coordination may do next.
* The decision function **never reads the field**. A test proves the decision is
  identical with the advisory present and with it removed.

A synthesis that describes a superseded evidence set arrives with
`describes_current_inputs=False` — computed, rather than handed over with a
caveat a reader might skip. An absent synthesis implies nothing: absence is not
consensus, and it is not an obstacle either.

SIGNAL remains non-risk-binding. A sentiment change moves the context digest —
coordination noticed — and leaves `risk_input_digest` untouched.

---

## System stops

Two mechanisms exist and they are not equivalent.

The **kill switch** is configuration SENTINEL itself honours, and it applies to
this flow directly. Under it, no case is opened and no case-level eligibility is
reported.

The **recorded pause** is a durable column set when SENTINEL returns
`PAUSE_SYSTEM` — but it lives in the Phase 0 accounting subsystem, which the
TradeCase workflow has no link to and whose schema a workflow-only deployment
does not carry. It is read through an injected port rather than assumed, so a
deployment that runs both subsystems supplies one and a deployment that does not
says so by its absence.

**The gap is real and is stated rather than papered over:** nothing in the
TradeCase flow can currently *set* that pause, because the service that sets it
never runs here. Until that is wired, the kill switch is the stop that applies.

---

## Security boundary

`CommanderCapabilities = { lease, context }`. The context port offers one
question about one case.

Verified absent from the package by import graph and identifier scan: reasoning
provider, prompt, model; HTTP, RPC and every market, social and chain provider;
wallet, signer, private key, calldata, broadcast, executor, ledger; status
setter, forced transition, task creation, task retry, task deletion; risk
context, risk limits, risk construction; and `TradeIntent` — the one object that
would carry a size.

There is no live-execution capability to enable. Not disabled: absent.

No public route forces anything; every API route remains a read.

No Docker, no containers, no wallet, no signing, no broadcast, no live
execution.

---

## Autonomy status

Honest version:

**Implemented, and not running.** The intake service and the decision function
are complete and tested. `COMMANDER_WORKER_ENABLED` and
`COMMANDER_INTAKE_ENABLED` default to false, nothing in a startup path
constructs either, and configuration presence starts nothing.

There is deliberately **no per-case COMMANDER worker task** in this phase. The
audit found the runtime has no "succeeded without evidence" outcome, and the
only state-changing action a coordinator could take today is intake — which is
not a per-case action, because it runs before any case exists. Adding an
evidence type or a fourth outcome kind purely to make a worker claimable would
be building the mechanism before the responsibility.

### Remaining blockers before autonomous PAPER execution

1. **A requested trade notional.** Nothing produces one. This is a strategy
   decision, not a coordination one.
2. **A runtime launcher.** No process claims tasks or runs intake cycles.
3. **A TradeCase-to-execution bridge.** `PaperTradingService` has no TradeCase
   link; something has to connect an authorized case to a paper fill.
4. **A TradeCase-reachable pause.** The durable pause lives in a subsystem this
   flow cannot set.

EXECUTOR is not implemented and is not in scope here.

---

## Compatibility

No migration. Alembic head remains `0006`, and none of `0001`–`0006` is altered.
Concurrency correctness rests on the existing unique constraint and idempotent
open path rather than on new schema — which is why the identity had to become
worker-independent instead.
