# Phase 2K: FUSE — evidence synthesis without authority

FUSE reads what the other specialists recorded and states, in one place, what it
adds up to.

It is the only role that looks at other roles' findings. That is the whole
reason its limits are structural rather than conventional: a layer that
summarises four verdicts is one edit away from being a layer that decides
between them, and the distance between those two things is the subject of this
phase.

---

## What the repository actually said

The design here follows repository truth rather than the shape a synthesis layer
is usually assumed to have. Three things were already decided and were not
changed:

| Question | What the code said |
|---|---|
| Does a FUSE task exist? | Yes — `TaskDefinition(FUSE, "SYNTHESIZE_EVIDENCE", required=False)` |
| Did FUSE produce evidence? | **No.** No requirement, no evidence type, `authorized_task_type(FUSE)` returned `None`, and the task was unclaimable |
| Where does it sit? | **Before PULSE and ANCHOR.** The pre-existing `evidence_for_fuse` returned only `before_trigger=True, required=True` evidence |
| What could it do? | `FuseCapabilities = {lease, evidence}` — read-only, no submission port |

So FUSE is a **pre-trigger** stage. Its inputs are ORBIT's discovery, ATLAS's
on-chain findings, SIGNAL's sentiment and VECTOR's setup. TRIGGER and
LIQUIDITY_EXECUTION are deliberately **not** inputs: they do not exist yet when
this task runs, and reaching forward for them would either block on evidence
nobody has produced or synthesize a different case than the one the workflow is
at. A post-trigger synthesis, if it is ever wanted, is a separate stage with a
separate responsibility — not this one widened.

The phase adds what was missing: a `SYNTHESIS_EVIDENCE` type, a context port, a
submission port, and a deterministic synthesizer.

---

## It is not a vote

The failure this phase exists to prevent has a shape:

```python
# Nothing like this exists, and nothing like it will.
score = 0.3 * orbit + 0.2 * signal + 0.3 * vector + 0.2 * atlas
if score > 0.6: ...
```

Four specialists answering four different questions do not have commensurable
opinions. ORBIT's interest, SIGNAL's attention, VECTOR's geometry and ATLAS's
contract findings are not four readings of one quantity — they are not readings
of one quantity at all. Averaging them would invent a number nobody measured and
encode weights nobody chose.

So there is no count, no average, no weight, and **no score**. The output is four
lists and one disposition, and the schema has no field a score could live in:
`overall_score=92` fails validation rather than being ignored.

Concretely: positive sentiment cannot offset a holder-concentration failure.
Not because the weights are set carefully, but because there are no weights.

---

## Hard blockers dominate, and cannot be cleared

A hard blocker is derived from a source's own committed verdict — its
`acceptance`, or a blocking code it published itself. FUSE forms no opinion
about whether a finding *should* block; the specialist already decided that, and
re-deciding it would be a second authority over one question.

Nothing in the package removes a blocker, and the contract will not store a
synthesis that disagrees with itself: a disposition of `COHERENT` alongside a
non-empty blocker list fails validation. Editing the blocker out afterwards does
not launder the case either — the sources the synthesis cites still record
`BLOCKED`, so the forgery contradicts the evidence it names.

The derivation order is the argument:

1. **Gaps first** — something required that could not be read leaves the
   question unanswered, and an unanswered question is not a negative answer.
2. **Then blockers** — a source that measured something bad says so.
3. **Only then factors** — support and caution are observations about evidence
   that is already admissible.

Step three cannot influence steps one and two. The disposition is computed from
the first two lists before any factor is consulted.

---

### Blockers beside gaps

Both facts can be true at once, and both are kept.

Precedence is blockers, then gaps, then caution — deliberately, not
incidentally. A measured danger stops the case on its own merits whether or not
something else is also missing, so it is what gets reported. But the gaps are
**not discarded**: a consumer that only learned "blocked" would not know a
source was also absent, and one that only learned "insufficient" might conclude
no definitive problem had been found.

| Evidence | Disposition | `hard_blockers` | `unresolved_gaps` |
|---|---|---|---|
| ATLAS blocked, SIGNAL missing | `BLOCKED` | ATLAS blocker | SIGNAL gap |
| SIGNAL missing only | `INSUFFICIENT` | empty | SIGNAL gap |
| SIGNAL degraded only | `CAUTION` | empty | empty |
| all clean | `COHERENT` | empty | empty |

Nothing has to infer that an `INSUFFICIENT` reading might secretly also contain
a definitive blocker. A future COMMANDER tests `hard_blockers != []` — a list,
not a string and not a sentence. A degraded non-safety signal stays a caution
and never becomes a blocker to simplify that table.


## Unknown fails closed

The standing invariant survives intact, and in one place it turned out to be
stronger than expected.

The workflow already refuses to record on-chain evidence as `AVAILABLE` while
any integrity domain is unestablished. So an ATLAS finding with an unknown
holder count never reaches the synthesis as an admissible source at all — it
arrives as a **gap** and the case is `INSUFFICIENT`.

This was found by trying to write the weaker guarantee. The first implementation
had a caution factor for "unknown axis on accepted evidence", and the test for
it could not be made to pass, because that state cannot exist. The branch was
removed rather than kept: a rule that can never fire is not a second safeguard,
it is an untested claim that one exists.

There is no path by which "the other agents look positive" softens a missing
safety measurement.

---

## Non-safety evidence keeps its qualifiers

SIGNAL is required but not safety-critical, so a degraded social reading is a
reason to be careful rather than a reason the asset is dangerous.

What matters is that the qualifier travels with the reading. A `POSITIVE`
assessment drawn from a concentrated campaign is not the same fact as a
`POSITIVE` assessment drawn from broad organic attention, and a synthesis that
dropped the qualifier would silently promote the first into the second.

So a tension is recorded as **both halves**:

| List | Factor |
|---|---|
| support | `SENTIMENT_POSITIVE` — SIGNAL assessed sentiment POSITIVE with ELEVATED attention |
| caution | `SENTIMENT_QUALITY_DEGRADED` — the reading carries less weight than its wording suggests |
| caution | `SENTIMENT_BREADTH_THIN` — CONCENTRATED organic breadth |
| caution | `SENTIMENT_MANIPULATION_CONCERN` — ELEVATED |

Collapsing that to "neutral" would throw away the only part a reader needed. Two
lists rather than one signed scale, because a caution and a support are not
opposite ends of anything — a case frequently holds both at once.

---

## Freshness comes from the sources

`valid_until` is the **earliest expiry among the sources**, never a window
starting at synthesis time. `observed_at` is the **oldest** source observation.

This is the single most dangerous thing a summariser can get wrong. If freshness
were anchored to when the summary was written, re-running FUSE would launder
stale evidence into fresh-looking evidence — the facts would not have changed,
but their apparent age would have. A synthesis written at noon over evidence
that expires at half past expires at half past.

It follows that no summary outlives the setup it summarises.

---

## The derived-evidence lifecycle

FUSE is a view over four other pieces of evidence, and a view has a lifecycle an
observation does not. ATLAS looking at a contract produces a fact that stays
true until somebody looks again. FUSE produces a reading that stops describing
the case the moment one of its inputs is replaced.

The first implementation got half of that right and the other half wrong, and
the wrong half was invisible from either end on its own:

* an **in-flight** synthesis of replaced evidence was correctly refused;
* but once a synthesis was **recorded**, the task was `SUCCEEDED` forever. No
  fresh reading could ever be derived, and the stale one stayed current and
  `ACCEPTED` while pointing at superseded sources.

Proved by probe rather than by reading: after superseding SIGNAL, the stored
synthesis reported `STILL CURRENT: True`, `status AVAILABLE`,
`acceptance ACCEPTED`, and `sources match live? False`. A consumer reading
current synthesis evidence would have received a reading of an evidence set the
case had left behind, presented as valid.

### Re-arming

A task definition may now declare `derived_from` — the evidence types its output
is derived from. Exactly one task declares it, and four bounds keep it from
becoming a cascade:

| Bound | Why |
|---|---|
| Only declared derived tasks | `derived_from` is empty everywhere else, so ATLAS finishing does not make SIGNAL runnable again |
| Only on a declared input | A synthesis is not an input to itself, so it cannot re-arm its own task and loop |
| Only before a trigger exists | Once the case is past the pre-trigger stage, re-answering that question is work queued to describe a stage nobody is at |
| Only a live, unexpired case | A terminal case is not re-derived into |

Re-arming reuses the task's own row and bumps its attempt counter, so a case
keeps one slot per role rather than accumulating one per revision. It also
clears the finished attempt's lease, which otherwise makes the re-armed task
look busy until that lease's own expiry.

Each new reading **supersedes** the last, so a case holds exactly one current
synthesis — the one describing the evidence it actually has.

### Same inputs, no churn

Re-derivation is driven by inputs changing, not by time passing. Recording the
same source set again produces no new task, and the synthesis is not among its
own declared inputs, so there is no loop.

---

## Freshness is revalidated at submission, not only referenced

Identity is not freshness, and a derived result needs both.

`_require_current_references` answers "were these the same envelopes?" — it
cannot answer "are they still usable?", because nothing was superseded when a
source simply ages out during computation. The context is built while every
source is current, the worker computes, and by the time the answer arrives the
window has closed. Without a second check the result would be stored claiming a
currency its inputs no longer have.

So a submission whose own `valid_until` has already passed is refused with
`EVIDENCE_STALE` — deliberately a different code from `TASK_SUPERSEDED`, which
means somebody replaced the inputs. Here nobody did; time simply passed.

A second expiry binds alongside the envelopes'. VECTOR states when the geometry
stops being true, and that instant can arrive well before the envelope carrying
it goes stale — so `valid_until` is the earliest of every source envelope's
expiry **and** the setup's own. Without it a synthesis could outlive the setup
it describes while still looking current, and no identity check would catch it,
because the setup's evidence id never changed.

---

## Supersession

A synthesis names every envelope it read, by id and by submission fingerprint.
The fingerprint is what makes the reference checkable: it says not "ATLAS
evidence" but "that exact ATLAS finding".

The authorization layer refuses a submission whose sources have since been
superseded. If FUSE reads setup A and VECTOR replaces it with B while the answer
is in flight, recording it would leave a current-looking synthesis describing a
setup nobody is trading any more. `_require_current_references` was extended
from the two singular reference fields to also walk `source_evidence_ids`, so
all four sources are checked rather than none.

---

## Risk transitivity: the decision that shaped the phase

This is the subtlest question here, and getting it wrong would have quietly
undone a Phase 2F decision.

`risk_input_digest` hashes exactly `policy.safety_types`. DISCOVERY and
SENTIMENT are deliberately excluded — they gate the workflow through
required-evidence blockers, but a change to either moves a case out of an
authorized state through the evaluator rather than through the risk snapshot.

Now consider a **safety-critical** FUSE. Its evidence would enter the risk
digest, and its fingerprint covers its synthesis — which mentions SENTIMENT.
A change in social data would then silently invalidate a risk authorization
through the summary layer. Phase 2F decided sentiment does not bind risk; a
summariser must not be able to overturn that decision by summarising.

So FUSE evidence is:

* **not safety-critical** — it never enters `safety_types`, so SENTIMENT cannot
  become risk-binding transitively;
* **not required** — a synthesizer outage cannot block a case whose canonical
  evidence is complete.

Both are asserted by test, including the control: changing ATLAS *does* move the
digest, so the mechanism works and simply excludes the right things.

The consequence is that a synthesis is recorded, superseded and audited like any
other evidence, and **gates nothing**. The evaluator skips non-required
requirements entirely. That is the correct weight for commentary, and it keeps
two further rules true without extra machinery:

* SENTINEL continues to read the canonical safety evidence directly. FUSE is an
  additional layer, never a replacement source for risk facts.
* ATLAS, VECTOR, PULSE and ANCHOR keep their direct risk binding. Summarising
  them does not weaken it.

---

## Advisory derived evidence

Stated plainly, because the flags alone do not convey it.

FUSE is **advisory derived evidence**. It is recorded, superseded and audited
like anything else, and it gates nothing. It is not a workflow gate, not a risk
gate, not a source-of-truth replacement, not required for SENTINEL, not required
for PULSE, and not an approval stage. The canonical workflow already enforces
the underlying required evidence; a summary should not become a second
availability dependency on top of it.

`required=False` was not changed in this audit and should not be changed to make
FUSE feel important. Source evidence stays authoritative.

### Implemented is not the same as running

The FUSE task is created `PENDING` at case-open like every other task, and a
registered FUSE worker can claim it, so this is not dead code and it does not
wait on COMMANDER.

What does not exist is any launcher: nothing in a startup path constructs a FUSE
worker, `FUSE_WORKER_ENABLED` defaults to false, and no process claims tasks on
its own. FUSE is therefore **implementation-complete and operationally
disabled**, exactly like every specialist before it. It runs when something runs
it.

### The future COMMANDER contract

Documented, not implemented.

A future COMMANDER may consume a synthesis as advisory context, and must never
trust it blindly:

* before using one, it must check that the synthesis's `source_evidence_ids`
  still match the current ORBIT, ATLAS, SIGNAL and VECTOR evidence;
* if the synthesis is **absent**, it must not infer positive consensus — absence
  of a reading is not a clean reading;
* if the synthesis is **stale**, it must not use it;
* it must read `hard_blockers != []` as a list test, never by parsing prose or
  inferring from the disposition.

Source evidence remains available to every later stage. A materialized view does
not remove the facts it was built from.

---

## No model

Every input is already a structured verdict that a specialist committed to —
`holder_integrity`, `data_quality`, `manipulation_concern`, `blockers`,
`data_gaps`. Three of the four upstream specialists already used a model to
*produce* those fields.

A second interpretation layer would place a probabilistic opinion on top of
answers that are already settled, and leave nobody able to say afterwards which
of the two the system acted on. So there is no reasoning provider here, no
prompt file, and no provider setting — verified against the package's import
graph parsed as a syntax tree rather than searched as text, since the docstrings
name the forbidden things precisely because they are forbidden.

Determinism buys several guarantees for free that a model would have required
defending:

* **No hallucinated references.** Every evidence id in the output is copied from
  a source the server supplied; there is no mechanism to author one.
* **No prompt injection surface.** The bounded views carry codes and enums —
  never a specialist's prose. Advisory summaries, cited observation ids and
  narrative tags stay in the source payload. Text inside evidence is never
  interpreted, so an instruction embedded in it has nothing to reach. The test
  asserts the derived output is identical with and without the instruction,
  which is stronger than asserting a validator caught it.
* **Replay is trivially idempotent.** The same evidence yields the same
  synthesis, the same fingerprint, and therefore one piece of evidence. Two
  workers reading the same case agree exactly, so there is no second valid
  answer to reconcile.

---

## No authority

The schema has no field for approval, rejection, position size, notional,
quantity, slippage, route approval, execution or a risk outcome — not unset,
**absent**, so an attempt to add one fails to validate.

`FuseDisposition` shares no member with `RiskOutcome`. `COHERENT` must not be
readable as an approval, and `BLOCKED` must not suggest SENTINEL has spoken.

| Disposition | Meaning |
|---|---|
| `COHERENT` | everything required is present, usable, and nothing contradicts anything |
| `CAUTION` | usable, with something worth saying out loud first |
| `BLOCKED` | a source states a fact that stops the case on its own merits |
| `INSUFFICIENT` | something required is missing, stale or unknown |

---

## Security boundary

`FuseCapabilities = { lease, context, submit }` — the same shape as every other
specialist. Phase 2K added the submission port and exactly one authorized
evidence type to use it with.

The context port offers one question about one case. There is no method to fetch
evidence by id, by type, or for another case, so a synthesizer cannot choose the
inputs that suit its conclusion — admissibility is decided on the server before
the worker runs. Verified absent from the package: session, HTTP client, RPC
client, provider handle, wallet, signer, private key, calldata, broadcast,
executor, ledger, SENTINEL, risk binding, and any ability to force a
`TradeCase` status.

The worker is disabled by default and nothing in a startup path constructs it.

No Docker, no containers, no wallet, no signing, no broadcast, no live
execution.

---

## Compatibility

No migration. Alembic head remains `0006`, and none of `0001`–`0006` is altered.

`evidence_type` is a plain `String(60)` column with no constraint or enum, so a
new value needs no schema change. The payload union gained a discriminated
member; every existing payload parses and replays unchanged, and a synthesis
payload without its structured detail parses too.

One superseded method was removed rather than left beside its replacement:
`TradeCaseService.evidence_for_fuse` returned raw envelopes and raised when
anything was unusable. The context reader supersedes it with bounded views and
surfaces unusable evidence as typed gaps, which is what lets a blocked or
incomplete case be *recorded* rather than merely refused. Two paths deciding
admissibility would be two places to get it wrong.
