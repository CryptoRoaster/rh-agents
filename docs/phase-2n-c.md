# Phase 2N-C — Bounded market acquisition

An explicitly started PAPER run may now **ask the market provider for
observations** before it trades, through the adapter, normalization and recorder
that already existed, and then read them back through `MarketReader` exactly as
it always did.

No daemon, scheduler or periodic background operation. **A successful
acquisition authorises nothing and guarantees no fill.**

## What was already connected, and what was not

Read before anything was written.

**Executable and connected:** `GeckoTerminalTransport` with its request, attempt,
concurrency and timeout budgets; `NetworkDirectory` with its bounded page scan
and its verified chain→network association; `GeckoTerminalAdapter.discover()`
and `normalize()`; `MarketRecorder` with database-enforced event identity;
`record_pair` as the shared discovery binding; `MarketReader` with its freshness
and availability contracts; and `src.markets.ingest`, a one-shot CLI that
records what discovery finds.

**Not connected:** *nothing a run could do made a market be recorded.* A bounded
PAPER run read whatever the last manual `python -m src.markets.ingest --once`
had written, however long before, and a case whose market had aged out stayed
blocked until a human ran that command again. There was also no way to observe
one specific market at all: discovery answers "what is new on this chain", and a
market this system already holds a case or a position in stops being new almost
immediately — which is precisely when its reading needs renewing.

## The architecture decision

### How acquisition is switched on

`PAPER_RUNNER_MARKET_ACQUISITION_ENABLED`, **off by default** and deliberately
separate from `PAPER_RUNNER_ENABLED`: consenting to a run is not consenting to
that run calling a public API. Settings refuse at boot, before anything can
write, if it is enabled without the run, or with `MARKET_PROVIDER=fixture`, or
with an acquisition time budget that could outlast the run.

`src.markets.ingest` is untouched. It keeps its own permissions, its own
discovery-only semantics and its own summary, and it cannot reach the targeted
read. Nothing in `src/api` reads the flag, constructs the stage or starts a run.

### Which markets are needed

A deterministic list, decided from durable state **before a single request**, in
one priority order:

1. **the market of every open position** — a holding that cannot be marked makes
   SENTINEL refuse *every* case with `PORTFOLIO_MARKS_UNAVAILABLE` rather than
   judge a portfolio it can only value in part;
2. **for each non-terminal case, oldest first:** its own market, then the market
   that prices its payment asset in dollars — ANCHOR sizes its ladder in the
   token that would actually be spent, so it needs that token's own USD reading
   and refuses to infer one;
3. **bounded discovery**, with whatever budget is left.

Needed data comes before new work on purpose: a run that spent its provider
budget discovering markets while its existing cases starve is a run that never
finishes anything.

**Case work set and data dependency are different questions.** The list of
markets is bounded by the *market* budget, never by the run's case budget:
acquiring the data a case depends on is not working that case. Where the market
budget does not reach, the market is reported `NOT_ATTEMPTED` and the dependent
trading step stays blocked by the contract that already blocks it.

One market wanted by two things is **one request**. The second need is answered
by that same reading and is reported as the replay it is.

### When the trading half may proceed

Acquisition is a prelude, never a permission. It gates only in negative
directions:

- **a system stop, read before any provider call.** Paused, or a stop that
  cannot be read at all, means nobody is called and the pass ends —
  `SYSTEM_STOPPED`, and an unreadable stop is additionally a technical failure,
  because unknown is not permission and is also not a healthy deployment. The
  question is itself bounded by the nearer of the acquisition and run deadlines,
  and is not asked at all once that has passed: it is a read against a database
  that may be unreachable, and an unbounded await on one would hold the whole
  pass open for as long as that database liked. A query cut off that way is both
  facts at once, and both are reported — `SYSTEM_STOP_UNREADABLE` with
  `TIME_BUDGET_REACHED` as its detail. The cancellation is awaited, so nothing
  continues against the database afterwards;
- **an outcome nobody can state.** A recording call cut off after it may have
  committed ends the pass *before any further mutating trading stage*, with
  `ACQUISITION_OUTCOME_UNKNOWN`. Everything after it would be a decision taken
  over data whose provenance this run cannot state;
- **a stage that was switched on and cannot be performed** — more chains named
  than the provider configuration permits, for instance. Reported as
  `CONFIGURATION_REFUSED` and `ACQUISITION_NOT_CONFIGURED`, and the pass ends:
  carrying on would trade over whatever happened to be recorded while reporting
  an acquisition that never ran.

Anything else — a provider that failed, a budget that ran out, a market that
came back unavailable — leaves the run to proceed exactly as it would have
without the stage. Whatever was recorded is recorded; whatever was not leaves
its dependants blocked.

## Identity and provenance

The **full recorded market identity** is used throughout: provider, chain,
network, pair, both assets, venue and pool locator. Never a symbol, never a
name, and never a pool chosen by ranking or search.

- A case supplies its own canonical `MarketIdentity`, stored when it was opened.
- A position names its market by pair, chain, network and provider; the full
  identity is looked up from the recorded observation and then **checked against
  those four fields**. A disagreement is `MARKET_IDENTITY_MISMATCH` — two
  providers observing one pool are two sources.
- A payment asset's market is a recorded market in which that asset is the
  **base**, because only such a market prices it. Where several exist, the most
  recently observed one is taken — a stated rule rather than whichever row the
  database returned first.

`MarketReader.identities()` is the new read that supports this, and it is
deliberately *not* a market reading: it returns coordinates only, never a price,
a liquidity figure or an availability claim, which is why it may ignore age. A
market whose last reading aged out is exactly the one that needs observing
again.

**The answer is bound to the planned identity.** For every market the run
*planned*, the canonical identity the provider's answer normalizes to is
compared with the identity that was planned, before anything is recorded —
through `MarketIdentity`'s own equality, not a second definition. This is not
covered by the checks under it: `observe()` compares the pair identifier, which
for a pool address carries the chain, the network and the pool and nothing else,
and `record_pair_reporting` compares the snapshot with the pair from the *same*
answer, which agrees with itself by construction. An answer that kept the
requested pool and named a different base asset, payment asset or venue is
therefore refused with `MARKET_IDENTITY_MISMATCH`, nothing is written, and the
market is left exactly as it was. Nothing is repaired, mapped or guessed. A
discovered market has no planned identity to be held to: it *is* what the answer
said, and intake judges it under its own rules.

**Refused rather than substituted**, each with its own code and no request made:
`PROVIDER_NOT_CONFIGURED`, `CHAIN_NOT_CONFIGURED` (never served from another
chain), `NETWORK_NOT_SUPPORTED`, `POOL_LOCATOR_UNKNOWN`, `FIXTURE_MARKET`,
`MARKET_NEVER_RECORDED`, `POSITION_MARKET_UNKNOWN`. At the provider boundary a
pool that was **not asked for** ends the read as an identity error — a provider
does not get to decide which markets this system observes — and a pool that was
asked for and did not come back is reported `MARKET_NOT_RETURNED` and left
exactly as it was.

Source instants and provenance are the adapter's, unchanged: `observed_at` is
the instant the fetch completed, as it always was. Reading a market again, or
recording the same event again, makes nothing fresher, and an observation that
is unavailable, in the future, contradictory or too old stays unusable under the
contracts that already say so.

## Budgets

Acquisition budgets are kept **apart** from the candidate, new-case, case and
worker-step budgets. They bound a different cost, paid to a different party, and
one number covering both would mean tightening provider spend by quietly doing
less analysis. Every one of them is reported.

| Bound | What it limits |
| --- | --- |
| `…_MAX_MARKETS` | distinct markets observed again in one run |
| `…_MAX_DISCOVERY_REQUESTS` | bounded discovery reads, across all chains; `0` is meaningful |
| `…_MAX_PROVIDER_REQUESTS` | logical provider requests, **including helper queries** |
| `…_MAX_HTTP_ATTEMPTS` | HTTP attempts, **including retries** |
| `…_MAX_SECONDS` | the whole stage, monotonic |

The provider's own budgets are **reused, not duplicated**: the transport is
built with `min(provider setting, run setting)` for requests, attempts and the
single-wait timeout, so a run can only ever tighten what the provider
configuration allows. Network resolution therefore spends the same budget a pool
read does, and a run whose request budget is exhausted by the directory is
refused by the transport itself with `REQUEST_BUDGET_EXHAUSTED`. The stage's
time window is additionally held to whatever is left of the run's own monotonic
deadline.

**Every bound is applied to the request, never to the response.** The discovery
read asks for at most as many pools as this run may still record, and the
targeted batch is fixed before it is sent. Nothing fetched is ever thrown away:
truncating a response means paying for observations and discarding them.

**The market budget is committed when work is triggered, not when it succeeds.**
One slot per distinct market as the targeted request goes out, and, for a
discovery read, the size of the answer it was permitted to return — reserved
before it runs, because the request was made under that permission whatever
comes back. A market that was not returned, or was returned and refused, keeps
its slot: releasing it would let one market's budget buy a second request, and
the work the first one cost has already been done. Deduplication is unaffected —
two needs pointing at one market are one request and one slot.

Four kinds of counter, kept apart: `provider_requests` and `http_attempts` are
the transport's own (the second includes retries); `requested` and
`budget_spent` are markets, counted when the request is issued, so a read that
fails afterwards cannot make the asking un-happen; `recorded`, `unchanged`,
`refused`, `failed`, `unknown` and `not_attempted` are results.

Cancellation is awaited and the transport is closed on success, failure and
cancellation alike, so no background work and no connection pool outlives the
stage.

**No provider request is made while a lock is held.** The plan is read from
durable state in short read-only sessions, all of which are finished before the
transport is built; the stage takes no account lock and no case lock at any
point. Proved from the other side: a separate transaction takes the paper
account and every trade case row `FOR UPDATE NOWAIT` while the provider request
is in flight.

## What the summary distinguishes

Six answers per market, because collapsing any two would let a run report
progress it did not make: **`RECORDED`** (this run's own write, confirmed durable
by the recorder), **`UNCHANGED`** (a replay — the event was already stored, or the
same market was already observed in this pass), **`REFUSED`**, **`FAILED`**,
**`UNKNOWN`** and **`NOT_ATTEMPTED`**. Beside them: how many markets were
*requested*, and the provider transport's **own** counters for logical requests
and HTTP attempts, so a retry is visible rather than hidden inside one number.

`MarketRecorder.record_reporting()` is what makes `RECORDED` honest. It reports
whether *this* call performed the insert, via `RETURNING` on the conflict
clause — a second `SELECT` could not tell the difference, because the row is
there either way. **No foreign commit is ever credited to a run.**

Nothing spans a provider call: each observation is its own recorder transaction,
and a run cut off halfway leaves every confirmed recording in place.

## Essential proofs

Production composition, real services, native PostgreSQL. External HTTP
responses are fixtures at the transport boundary; no real provider or model call
is made.

**Actually executed:** `GeckoTerminalTransport`, `NetworkDirectory`,
`GeckoTerminalAdapter` (`discover` and `observe`), `normalize`, `MarketRecorder`,
`MarketReader`, `CommanderIntakeService`, `TradeCaseService`,
`WorkerRuntimeService`, `WorkerRunner`, the ORBIT, ATLAS, SIGNAL, VECTOR, FUSE,
PULSE and ANCHOR handlers with their real context readers, `RiskRequestService`,
`src.risk.engine.evaluate`, `CaseFillService`, `PaperExecutor`, the ledger, and
the whole composition through `build_stack`.

**Replaced at the outside edge, in test code only:** the HTTP response bytes
(`httpx.MockTransport`), the reasoning model, the chain read, the holder and
origin indexers, the social source, the market structure series and the quote
source. **A fixture test is not evidence that the real provider works.**

| Proof | Test |
| --- | --- |
| Provider response → adapter → recorder → reader → intake → real specialists → risk request → PAPER fill, with no market row written by the test | `test_a_provider_response_becomes_a_recorded_market_a_case_and_a_fill` |
| An existing case outside the current discovery list is updated by locator | `test_an_existing_case_outside_discovery_is_updated_by_locator` |
| One market wanted twice: one request, and the second reported as a replay | `test_one_market_wanted_twice_is_observed_once_and_replayed` |
| An open foreign position is valued from its own acquired market | `test_a_foreign_open_position_is_valued_from_its_own_acquired_market` |
| A needed source that cannot be acquired prevents every fill | `test_a_holding_whose_market_cannot_be_acquired_prevents_every_fill` |
| A regular PULSE wait, new observations on the next explicit run, and the existing 2N-B evidence refresh beside them | `test_a_pulse_wait_a_new_observation_and_an_evidence_refresh_still_fill` |
| An old observation stays old; reading it again does not rejuvenate it | `test_reading_an_old_observation_again_does_not_make_it_fresh` |
| A new unavailable reading does not fall back to the usable older one | `test_an_unavailable_new_reading_does_not_fall_back_to_the_usable_old_one` |
| Identity mismatch, an unsupported chain, and a market never recorded are refused without a request | `test_a_holding_whose_recorded_market_disagrees_is_refused`, `test_an_unsupported_chain_is_refused_and_never_served_from_another`, `test_a_holding_whose_market_cannot_be_acquired_prevents_every_fill` |
| A pool that was not asked about is refused at the boundary | `tests/markets/test_targeted.py::test_a_pool_that_was_not_asked_about_is_refused` |
| Market, request and time budgets act before further work, including retries and helper queries | `test_the_market_budget_is_applied_before_the_request`, `test_a_full_market_budget_leaves_the_rest_visibly_unattempted`, `test_the_request_budget_stops_the_stage_including_helper_queries`, `test_the_provider_request_budget_is_the_lower_of_the_two`, `test_an_exhausted_time_budget_stops_before_the_request` |
| Partially confirmed recordings plus a failure: an honest summary, and what committed stays | `test_confirmed_recordings_survive_a_later_failure`, `test_a_provider_failure_keeps_what_was_already_recorded` |
| An unknown recording outcome stops before any mutating trading stage | `test_an_unknown_recording_outcome_stops_before_any_trading_stage` |
| A stop in force, and an unreadable stop, spend no provider request | `test_a_paused_system_is_not_asked_to_spend_a_provider_request`, `test_an_unreadable_stop_is_reported_as_a_fault_and_stops_the_pass` |
| A stage that cannot be performed ends the pass rather than being skipped | `test_a_stage_this_configuration_cannot_perform_ends_the_pass` |
| An answer that keeps the asked pool but swaps the base asset, the payment asset or the venue is refused, and nothing is written | `tests/runner/test_acquisition_hardening.py::test_a_swapped_base_asset_under_the_asked_pool_is_refused`, `…_quote_asset_…`, `…_venue_…` |
| A market asked about and not returned, or returned and refused, still costs its budget — including across chains | `…::test_a_market_asked_about_and_not_returned_still_costs_its_budget`, `…::test_a_refused_recording_still_costs_its_budget`, `…::test_a_spent_budget_stops_the_second_chain_before_its_request` |
| Discovery commits the capacity it was allowed to bring back, and a doubled need still costs one | `…::test_discovery_reserves_what_it_is_allowed_to_bring_back`, `…::test_one_market_wanted_twice_still_costs_one` |
| A failed read does not erase the markets it asked about | `…::test_a_failed_read_does_not_erase_the_markets_it_asked_about` |
| An expired deadline asks no stop source; a stop source that hangs is cut off, awaited, and ends the pass with nothing traded | `…::test_an_expired_deadline_never_asks_the_stop_source`, `…::test_a_stop_query_that_hangs_is_bounded_and_ends_the_pass` |
| No database lock is held across a provider request | `tests/runner/test_acquisition_concurrency.py::test_no_provider_request_is_made_while_a_row_is_locked` (PostgreSQL only) |
| Restart and concurrent runs stay recorder-idempotent, with at most one fill per order | `tests/runner/test_acquisition_concurrency.py` (PostgreSQL only) |
| A historical replay is answered with no new acquisition | `test_a_historical_replay_needs_no_new_acquisition` |
| The API starts neither acquisition nor a run | `test_the_api_starts_neither_acquisition_nor_a_run` |
| The recorder says which write was its caller's | `tests/markets/test_targeted.py::test_the_recorder_says_which_write_was_its_callers` |
| Identity lookups answer for a market that has aged out, and only where the asset is priced | `tests/markets/test_targeted.py` |

## Migration

**None.** Nothing was added to the schema and no durable contract required one;
`alembic check` reports no new upgrade operations and the head stays at `0011`.
The acquisition writes market observations through the existing recorder into
the existing table, and reads cases and positions through columns that already
exist.

## Deliberately not done

- **`GECKOTERMINAL_MAX_DETAIL_LOOKUPS` stays `0`, and stays a `Literal`.** It
  bounds the *discovery* pipeline, where `snapshot()` still makes no request and
  every value comes from the discovery document. The targeted read is a new,
  separately named, separately switched and separately budgeted capability, and
  the standalone ingestion command cannot reach it. Nothing about the old bound
  was loosened to make room for the new one.
- **No market observed before pool locators existed can be targeted.** Such a
  row has no recorded locator, and reading an address back out of a derived
  identifier is inventing coordinates. Reported as `POOL_LOCATOR_UNKNOWN`.
- **No third refreshable evidence origin.** The workflow policy is unchanged. A
  market is refreshed by *acquiring an observation*; evidence is refreshed by
  asking the task that produces it to run again. They compose and are proven to,
  but they remain two different mechanisms.
- **No relaxed limit anywhere.** SENTINEL's source bound, the reader's freshness
  window, PULSE's interval, ANCHOR's tolerances, the intake rules, the provider
  budgets and the cost assumptions are all untouched. Acquisition success is not
  an approval.
- **No retry of a negative assessment, no automatic exit or re-entry, no public
  write API, no live trading, signing, broadcast or Docker.** No general
  refactoring: the recorder gained one reporting surface over its existing
  single implementation, the reader one identity-only lookup, and the adapter
  one targeted read that reuses the same normalization.
- **The PULSE/SENTINEL interaction from 2N-A remains.** A trigger found on a
  rescheduled monitor check arrives with on-chain evidence older than the risk
  engine accepts; the 2N-B source refresh is what answers it, and this phase
  neither widens nor narrows either policy.
