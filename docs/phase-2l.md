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

## What an independent review found

Six defects were reproduced against the pushed branch and are fixed here. Each
had a working reproduction before it had a fix.

### An expired authorization read as current

`matches_current_inputs` compared only digests, so an approval SENTINEL issued
with a two-minute life still reported `RISK_CURRENT` eight minutes later.

Worse, the context trusted the **stored** case row. Time moves without writes, so
a case whose authorization aged out kept a stale `RISK_APPROVED` until something
touched it — and even after an evaluator run corrected the row, the decision
still said current.

Both halves are fixed. Identity and validity are now separate fields, and the
status is recomputed from current state by the *same* evaluator the workflow
persists from, called read-only. That is reuse rather than a second engine:
there is one requirement table, one transition matrix and one freshness rule,
and this reads them at the present instant instead of at some earlier one. A
test asserts the case revision is unchanged after repeated reads, and another
asserts the read-only view agrees with a persisted evaluation.

Pinned at the microsecond either side of expiry.

### Intake could never move past a terminal case

The key was `commander-intake:{version}:{pair_id}` — constant per market
forever. After a cancellation the same candidate returned the **old cancelled
case**, counted as newly opened; a new observation of the same market produced a
different open fingerprint and raised `IDEMPOTENCY_CONFLICT`, which escaped and
aborted the entire cycle.

The key is now scoped to the **generation**: the market, plus the case that last
ended for it. Everyone computing it sees the same predecessor and derives the
same key, and it changes exactly once per generation — never per observation and
never per worker. Three separate questions are now asked separately:

| Question | Answer |
|---|---|
| Is this a replay of the same intake event? | same key → `ALREADY_OPENED`, not counted |
| Is a case already live for this market? | any non-terminal case → `ACTIVE_CASE_EXISTS` |
| May a new generation start? | only after `EXPIRED` or `CANCELLED` |

`RISK_REJECTED` explicitly does **not** permit a new generation. A rejection is a
verdict about the market's current state, not a case that ran out of road, and
re-observing a market carries no new information about risk — letting a fresh
fetch start another attempt would be retry-until-pass with extra steps.

A workflow refusal is now caught per candidate, so one unopenable candidate
cannot take the rest of the pass down with it.

The active-case check deliberately asks about *any* live case rather than about
the newest row: two cases opened in the same instant tie on `opened_at`, and
asking "is the newest one active" would let a live case hide behind a terminal
sibling.

### A stop arriving mid-cycle was already past the gate

The pause was read once at the top of `run_cycle`. A stop coming into force
while candidates were being read had already been passed, and a case was opened
under it. The window cannot be closed by reading earlier — only by reading last,
immediately before the one write this service performs. It now is.

Three further corrections in the same area:

* **Unknown fails closed.** A missing pause port, a missing account row and a
  control that raises were all answered `False`, turning an unavailable stop
  into a green light. All three now mean paused.
* **The authoritative account.** `AccountPauseReader` selected whichever row came
  first. `paper_accounts` is singular by constraint (`id = 1`) and every other
  reader addresses it by that identity.
* **The real mode.** `trading_mode` was hard-coded to `PAPER`, so the field
  described an intention rather than the deployment. It is now the configured
  mode, constrained to `OBSERVE` and `PAPER` — `LIVE_AUTONOMOUS` and anything
  unrecognised fail to validate rather than arriving as a value some later
  branch might honour. **OBSERVE halts progression**, reported as its own reason
  so an operator does not go looking for a kill switch nobody pulled.

The `COMMANDER_KILL_SWITCH` comment claimed it was "the deterministic stop
SENTINEL already honours". It is not: `RiskLimits.kill_switch` is a different
field this one is not wired to. The comment now says what the flag actually
reaches, and a test asserts the two are not described as connected while they
are not.

### An expired synthesis looked usable

The advisory check asked only whether the cited evidence was still current.
References stay intact while a reading ages out — and because a synthesis
expires with the earliest of its sources *and* the setup it describes, an
expired one can outlive an unchanged evidence set. `expired` is now computed
alongside `describes_current_inputs`, and `is_usable` requires both. The
decision still never reads either.

### A stale submission stopped the worker

`EVIDENCE_STALE`, added in Phase 2K, was missing from the runner's
expected-refusal table. `_record_refusal` re-raised it, the raise propagated
through `run_once`, and `run()` has no guard around it — so the polling loop
ended with the task still `RUNNING` until its lease expired. A worker that
stopped without saying so.

It is now recorded as `TRANSIENT`: retryable, so a fresh attempt reads fresh
inputs, and **budgeted**, so a persistently-too-slow worker stops rather than
retrying forever. `TASK_INVALIDATED` would have been the unbounded version —
superseded outcomes are not counted against the retry budget at all.

### Identical evidence recomputed a second later conflicted

`SynthesisDetail` carried its own `evaluated_at`. The submission fingerprint is
computed over the payload, so two recomputations of identical evidence produced
one idempotency key with two fingerprints, which the runtime correctly reports
as `RESULT_CONFLICT`. When the work happened is already recorded authoritatively
on the envelope as `created_at` and `recorded_at`; repeating it in the payload
bought nothing and cost replay.

**The same defect existed in ANCHOR** and was not reported. Checking rather than
assuming found it: `ExecutionAssessmentDetail` carried the same field, with the
same consequence. Fixed identically.

Checking ANCHOR also turned up a second, related problem: its evidence
`valid_until` was `evaluated_at + max_reference_age` — anchored to the **run**
rather than to the sources. That is precisely the laundering FUSE was corrected
for in Phase 2K: re-assessing the same quotes extended the evidence's life
without any fact getting newer. It is now measured from the reference
observation.

Both have regression tests, each paired with a control proving genuinely changed
content is still detected as different.

---

## Second review round

Four further defects were reproduced and closed.

**A pause arriving mid-cycle still opened a case.** Moving the check later only
narrowed the window; it could not close it, because between any read and the
insert there is a gap and a pause committed inside it has already been passed.
The opening now happens in one transaction that first takes the same account
row lock the paper service takes before setting the pause, so the database
orders the two rather than timing doing it. **Lock order is paper account, then
trade case** — safe because the workflow locks trade cases and never touches the
account, and the paper service locks the account and never touches a trade case,
so no cycle is introduced.

Opening is additionally serialised on the intake key itself, with a
transaction-scoped advisory lock. Relying on the account lock alone would have
been a guarantee that quietly disappeared wherever the pause source was stubbed
or absent — which is exactly how an injected boolean port can look like a
concurrency solution without being one. The proofs use the real account-backed
reader against a schema carrying the accounting tables, not a stub.

**Historical payloads stopped parsing.** Removing `evaluated_at` from two models
that forbid extra fields made every row written before the change unreadable,
and evidence is append-only. The field is restored as `legacy_evaluated_at`,
optional, accepted under both spellings and never written by any code path — so
new submissions stay deterministic, which is what made removing it necessary,
while stored ones keep what they were recorded with. A blanket `extra="ignore"`
would have achieved the parsing and lost the contract: every typo would vanish
with it. Reading, round-tripping, new-versus-new replay and a genuine content
change are tested separately, with fixtures written as the previous model
produced them rather than as the current one emits.

**Two workers claimed one case between them.** Opening is idempotent by key, so
a caller receiving only the case cannot tell a creation from a replay — and the
loser of a race counted the winner's case as its own opening, spending two units
of a budget that bounds new cases. The session-joining open now reports whether
it created, and only creations are counted. Tested on the sum rather than on the
set of case ids, which is what made the original miscount invisible.

**A decision could be made from a context that had aged out.** The context
freezes every temporal fact at read time, so `expired=False` stayed false
forever and a later decision repeated a verdict about a moment that had passed.
A context now carries the earliest *future* expiry among the case, the
authorization, each envelope and the setup's own horizon, and a decision made
past it is refused as `STALE_CONTEXT` — demanding a fresh server-built view
rather than extending anything. Expiries already past when the view was built
are excluded, since they have already changed what the case means and that
change is what the view records.

---

## Compatibility

No migration. Alembic head remains `0006`, and none of `0001`–`0006` is altered.
Concurrency correctness rests on the existing unique constraint and idempotent
open path rather than on new schema — which is why the identity had to become
worker-independent instead.
