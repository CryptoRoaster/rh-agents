# Phase 2M-G — Explicit paper re-entry

One narrow server-side call opens a new trading cycle after a completed PAPER
exit. It opens a TradeCase. That is all it does.

No automatic reopening, no trading strategy, no cooldown and no new risk limits.
No partial sales, no topping up, no pool change. No stop-loss or take-profit. No
launcher, no autonomous loop, no public write API. No live trading, signing,
broadcast or Docker.

## Contracts audited first

Read before anything was written:

| Contract | What it said | Consequence |
| --- | --- | --- |
| `MARKET_BARRING_CASE_STATUSES` + intake | `EXECUTED` bars the market, but only via the *newest terminal* case | had to become "any case", or a lapsed successor would unbar it |
| `TradeCaseService.open_trade_case_in_session` | opens at `DISCOVERED`, records discovery, stabilises, joins a caller's transaction | exactly what a successor needs; no forced status |
| `positions.asset_id` unique | one row per asset, reused after an exit | the row cannot identify a cycle by itself |
| `PaperExitService._entry` | searched every executed entry for the asset | would report ambiguity on the second cycle |
| `trade_case_exits` uniqueness | one exit per position | had to become one exit per *cycle* |
| `replay_in_session`, key conflicts | identity from the key, conflicting content refused | reused for the re-entry key |
| lock order | paper account → trade case | unchanged |

**`EXECUTED` was not removed from the barring set.** Intake still refuses an
executed market; the successor comes from the explicit contract or not at all.

## The trading cycle

A cycle is a case that opened a position and the exit that closed it.
`trade_cycles` is one row per cycle, carrying the market, the sequence, the exit
it succeeds and the key that asked for it.

**`predecessor_exit_id` is unique**, so a completed cycle has at most one
successor. **`trade_case_id` is unique**, so a case belongs to one cycle, and a
cycle takes that case's identity as its own — `cycle_of` is plain equality, so
the migration that gave historical entries their cycles could say the same thing
in portable SQL rather than in a second, differently-spelled rule. The column
exists separately because other tables reference the *cycle*, and referencing
the case would say something subtly different: a case that never filled is not a
cycle, and the row is only written once one actually fills.

Two check constraints keep a half-described cycle out: a first cycle has neither
a predecessor nor a request key, every later one has both, and sequence 1 is
exactly the set with no predecessor.

**How a reused position row is told apart from history.** `positions` stays one
row per asset — the whole 2M-E valuation contract rests on exactly one holding
per asset — and carries `cycle_id`: the cycle that *currently* owns it. A fill
carries it forward unchanged; a reopened holding (quantity nil at the decision)
is restamped with the market and cycle of the order acquiring it, because a
closed position belongs to the cycle that closed it only as history. Everything
else about a cycle lives in `trade_case_executions`, `trade_case_exits`,
`trades` and `execution_results`, each now cycle-identified.

**The exit finds its own entry.** `PaperExitService._entry` reads the holding's
cycle instead of searching the market's history, so a second cycle resolves
exactly rather than reporting ambiguity. `trade_case_exits` moved its uniqueness
from the position to the cycle: two sales of one holding *inside* a cycle stay
impossible, one sale per cycle becomes possible.

## Re-entry preconditions

The caller names the predecessor's **exit** and an idempotent key. No quantity,
price, portfolio value, limit, risk decision or evidence crosses the boundary.
The successor opens on the same recorded market identity — no pool change.

Under the account lock, then the predecessor case lock:

- a recorded exit exists (**a closed position row is not an exit** — a row at
  zero looks exactly like one that was never opened);
- the exit, its cycle, its entry and its case all agree with one another;
- the predecessor case is `EXECUTED` — a risk rejection, an abandoned entry and a
  lapsed case each end a cycle without a completed trade, and none of them
  qualifies;
- nothing is held: quantity **and** remaining cost basis are nil, and the
  holding is attributed to the cycle the exit closed;
- no successor cycle exists yet;
- every stop allows it.

Each failure is a typed refusal that writes nothing.

## Workflow and evidence

The successor is opened through `open_trade_case_in_session` in the state every
case starts in. **No `force_status`, no direct `READY_FOR_RISK` or
`RISK_APPROVED`.** The old binding, risk request, intent and evidence stay where
they are and are never copied: the new case has no `risk_input_digest`, no
binding and no request, and a fill attempt on it is refused as
`REQUEST_NOT_FOUND`. Evidence must be submitted to the new case and pass the
existing freshness and safety rules.

The re-entry call opens the case and nothing else — no purchase, no worker call,
no loop. `ReentryOpened.authorizes_execution` returns `False` unconditionally,
stated as a property so the day that changes it is visible.

Identity namespaces are kept apart: the successor's idempotency key is
`rh-agents:paper-reentry:<key>`, which can collide with no intake generation, no
entry request and no exit order.

## Intake, refusals and stops

`_latest_terminal` no longer decides the bar. `_barred` asks whether **any** case
for the market has a barring status, for the same reason `_active_case` asks
about every live case: the newest-row ordering is a total order, not a statement
about what a market has already done. An executed cycle followed by a case that
expired would otherwise look unbarred.

While a successor is live, intake refuses it as `ACTIVE_CASE_EXISTS`, so no
parallel case can appear. A rejected successor is not retried: the predecessor
already has its one successor, and a second key is refused with
`SUCCESSOR_ALREADY_EXISTS`. No retry-until-pass, no automatic reset.

`OBSERVE`, a configured kill switch, an unreadable stop source and the durable
account pause all refuse a re-entry, the pause read from the locked account row.
No special rights anywhere.

## Transaction and replay

Locks: paper account (`FOR UPDATE`), then the predecessor case. Where two cases
are involved the predecessor is locked first — the successor does not exist yet
when the predecessor is checked, so the order is not a choice, and it is the
order this call always uses.

The precondition check, the successor case and the cycle link commit together or
roll back together. **No market or provider port is consulted at all**: nothing
here prices, values or judges anything, so there is no unbounded wait under a
lock.

Same key, same binding → the same successor case, `replayed=True`, nothing
re-opened. Same key, different predecessor → `REENTRY_KEY_MISMATCH`. Different
keys, same predecessor → at most one successor.

## Migration

`0011_trade_cycles`. One new table; `cycle_id` added to `trade_case_executions`,
`trade_case_exits` and `positions`; the exits' uniqueness moved from the position
to the cycle.

`trade_cycles.predecessor_exit_id` is deliberately **not** a foreign key: an exit
already points at its cycle, and pointing back would make the two tables mutually
dependent, which no schema tool can order and no fresh database can create. The
value is read under the account and case locks from the exit row itself.

**The backfill is a mapping, not a guess**, and set-based so it renders as
offline SQL. Every `trade_case_executions` row names one case and the case names
one market, so each existing entry becomes cycle 1 of its own market with no
predecessor; each exit inherits the cycle of the entry it already references. An
open position is attributed only where exactly one executed entry accounts for
it — where none or several do, `cycle_id` stays NULL and the exit path refuses
exactly as it refuses today.

## Test evidence

31 tests in `tests/reentry/`. The production path runs throughout: the real
workflow service against a real database, the real risk request, the real case
fill, `src.risk.engine.evaluate`, `PaperExecutor`, the real ledger postings and
the real exit service. Both cycles are real; nothing is inserted to stand in for
one, except in the migration tests, where the "before" state is seeded exactly as
the services wrote it.

Honestly distinguished: the **evidence is fixture evidence**, and the market
feed's read and the stop source are supplied as values — two ports, as values.
**No provider is called for real anywhere**, no specialist worker runs and no
launcher exists. No sleep is used for any time case.

- `test_integration.py` — the whole chain (entry 1 → exit 1 → re-entry → new case
  → new evidence → new risk request → entry 2 → exit 2) with both cycles' records
  separate and consistent and the second exit finding its own entry; entry 1 and
  exit 1 still replaying unchanged afterwards; an open holding, a lingering cost
  basis, a missing exit, disagreeing records and a cycle that never executed each
  refusing; one successor per exit, replay under the same key, a key naming
  another predecessor, and a rejected successor not retried under a new key;
  every stop and the durable pause; intake barred before and during the
  successor, and still barred after a successor expires; no inherited approval,
  binding, request or digest; and the position row naming the cycle that owns it.
- `test_concurrency.py` (PostgreSQL only) — two identical calls opening one cycle
  and replaying the other; two different keys opening at most one; a pause racing
  a re-entry; a failure before commit leaving no case and no cycle, with a later
  call still succeeding; and no market port on the service at all.
- `test_audit.py` — cycle two's stored basis recomputing the judged `RiskContext`
  after the account and holding change; cycle two booking its own cash, fees and
  day's loss; a holding whose cycle names another market refused by the exit; and
  a re-entry moving no money.
- `test_migration.py` (PostgreSQL only) — a schema at `0010` seeded as the
  services wrote it, then migrated: a completed cycle carried over with entry,
  exit and holding all in one cycle; an open holding keeping its entry; and an
  ambiguous holding left unattributed rather than guessed at.

Full gates: PostgreSQL 3237 passed / 20 skipped, SQLite 3159 passed / 98 skipped,
ruff, strict mypy, Alembic heads/current/check/offline SQL and a downgrade to
`0010` and back at `0011`, frontend typecheck/lint/format/build.

## Hardening round

Two defects, each reproduced against `96b7e8d` before being fixed.

**An absent record was read as an absent objection.** Every holding check sat
inside `if holding is not None`, so an exit naming a position row that could not
be resolved skipped all of them and a successor was opened for a predecessor
whose holding had not been shown to be closed at all. `trade_case_exits` carries
its position reference without a foreign key — the row outlives the cycle and is
reused — so a reference that leads nowhere is reachable. Six reproductions
returned `ReentryOpened` where a refusal was required: a dangling position
reference, a holding whose asset had changed, and one whose pair, chain, network
or provider had.

The checks are now unconditional, and there are more of them. A missing row is
`POSITION_NOT_FOUND`. Quantity **and** cost basis must both be nil. And the
holding, the cycle it names, the exit that closed it and the market of the entry
case must describe one trade: the cycle reference, the asset against the cycle,
the exit and the entry's own market, and the whole recorded market identity —
pair, chain, network and provider — not just the pair. Nothing is guessed or
repaired; a disagreement is reported and the call writes nothing.

The replay of an already-opened successor stays *before* all of it, deliberately:
that successor exists, and re-reading the holding now would answer a question
about today rather than returning what was recorded. So does the
one-successor-per-exit refusal.

**The downgrade could be started and not finished.** With two completed cycles
both exits reference the same reused position row — proved with real cycles, not
asserted about the schema — so restoring the old one-exit-per-position uniqueness
fails part-way through a migration that has already dropped things.

### The actual downgrade boundary

A lossless downgrade after re-entry is **not offered**, and is now refused before
any schema change rather than attempted:

- **Refused** as soon as any successor cycle exists — `sequence > 1` or a
  predecessor exit on file. An *opened* successor counts even if it never
  filled: the row is what records which exit it followed and under which key,
  and that mapping exists nowhere else.
- **Nothing is deleted, merged or renumbered** to make the old shape fit. That
  would destroy booked history, so the migration refuses instead and says to
  restore from a backup taken before the re-entry.
- **Supported** with first-cycle data only: the downgrade runs, the booked exits
  and positions survive it unchanged, and upgrading again rebuilds every cycle
  link from the backfill.
- **Online and offline are both covered.** Online the check queries and raises.
  Offline the generated script cannot query while it is written, so it carries
  the same condition as a `DO` block that raises in the database that runs it,
  emitted *before* the first `DROP` — a script that quietly dropped the table
  because nobody could run the check would be exactly the silent bypass this
  exists to prevent. For a dialect with no portable form for that guard,
  generation is refused rather than emitted without one.

Proved in `tests/reentry/test_downgrade.py` on native PostgreSQL: first-cycle
data downgrading and upgrading again; an opened successor and two completed
cycles each refusing with the schema, the `alembic_version` row, the cycle links
and the realised results all intact afterwards; the generated PostgreSQL script
carrying the guard ahead of every `DROP`; and generation refused for a dialect
that cannot carry it.

## Remaining limits

- **One successor per completed exit.** Whether a *further* attempt should ever
  be possible after a refused successor is a question this phase does not answer,
  and answering it accidentally — by letting a second key through — would be a
  strategy nobody wrote down.
- **Same market only.** A re-entry reopens the market that was closed; a
  different pool is a different market and has no contract here.
- **Nothing triggers it.** There is no cooldown, schedule, price condition or
  automatic reopening. A caller asks; this answers.
- **It opens a case and nothing more.** Evidence, sizing, the risk request and
  the fill all run again through the ordinary path.
- **No launcher and no worker.** Nothing calls this service automatically, and
  there is no public write API.
- Live execution, signing and broadcast remain out of scope entirely.
