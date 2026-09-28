# JEV-0: shadow triage of new discovery candidates

**Shadow only.** JEV writes evidence for later calibration. Nothing reads it for
a decision: not watch creation, the ORBIT queue or selection, the ORBIT budget,
checkpoints, the watch lifecycle (WATCHING / DORMANT / PROMOTABLE), VECTOR,
TradeCases, SENTINEL/risk, orders, fills, positions or execution.

## What changed after review (and why)

**Old:** JEV assessed only the watches a run opened. With ~20 valid pools per
run and a limit of 10 watches, a signal-blind round-robin decided which half
the cheap triage ever saw — the opposite of what an early-discovery triage is
for. **New:** JEV-0 is a candidate-level shadow assessment of **every new
valid discovery candidate**, fixed from discovery before, and blind to, the
watch allocation. Watch allocation stays a neutral round-robin for now, and
JEV has **no selection authority**.

## Where it runs

```
GeckoTerminal discovery (≈ 20 valid pools/run, both chains)
        │
        ├──► every valid pool recorded as a market observation (PostgreSQL)
        │
        ├──► JEV-0 candidate set frozen: every new stream, before watch allocation
        │
        ▼
watch allocation — TEMPORARY NEUTRAL WATCH ALLOCATION: ≤ 10/run, chains in turn,
        │          declined streams marked NOT_OPENED_AS_WATCH_DUE_TO_WATCH_LIMIT
        ▼
ORBIT (Codex) reviews, history checks — unchanged
        │
        ▼
JEV-0 calls for the frozen candidate set (watched and declined alike)
        │
        ▼
discovery_fast_assessments (PostgreSQL) → calibration
```

A **new candidate** is a valid stream in this run's discovery that had never
been observed before this run and has no watch. The set is taken from
discovery alone, before any watch is allocated; the calls themselves run
**last** in the run, after every ORBIT review and history check, so they
cannot change what the run decided, nor delay it. Streams known before JEV are
never backfilled. A failure, timeout, rate limit or store error stays in the
shadow tally: it is not a run error, triggers no retry and no Codex call.

**Round-robin is a TEMPORARY NEUTRAL WATCH ALLOCATION**, not an
early-discovery priority. It exists so the first configured chain cannot
starve the other (before the leak fix every BSC watch came through the
bootstrap leak). Any change to how the ten slots are chosen waits for the
shadow calibration.

**Declined** means only NOT_OPENED_AS_WATCH_DUE_TO_WATCH_LIMIT: the stream
keeps being observed when discovery sees it, and it gets its JEV-0 assessment.
It will not become a watch later — not by bootstrap and not by being
discovered again — so the slots of later runs go to genuinely new pools.

## Official API contract (verified 2026-09-28, docs.typesafe.ai)

| | |
|---|---|
| Endpoint | `POST https://api.typesafe.ai/v1/systemone` |
| Auth | `Authorization: Bearer <key>`; the official SDK reads `TYPESAFE_API_KEY` |
| Body | `state` (text/object/array), `model`, `questions` (map of typed questions) |
| Question types | `noul` (yes/no → probability), `choice` (≤ 255 options → choice, probabilities, confidence), `score` (2–10 ordered levels → probability-weighted score, legend, probabilities, confidence) |
| Response | `model` (the versioned id that answered), `answers` keyed like the questions, `usage.input_tokens/output_tokens` |
| Errors | 401 key, 422 invalid body, 429 rate limit, 529 overloaded |
| Models | `jev-1.13.0`; aliases `jev-latest`/`jev-preview` move with releases |
| Limits | 1,200 requests/min, 250k tokens/s (documented as dynamic) |
| Price | input tokens only, $0.042 per Mtok |
| Known weakness | Jev 1.13 is not a calculator: keep arithmetic in code |

Implementation choices that follow from it:

- Raw HTTP (`httpx`, already a dependency) rather than the SDK, because the SDK
  retries by default and this adapter makes **exactly one** attempt.
- `JEV_MODEL` must be a pinned version (`jev-1.13.0`); aliases are refused by
  `Settings`, so answers never change under a calibration silently. The
  answering version from the response is stored as `model_version`.
- All arithmetic (ratios, changes, magnitude bands) is done in code before the
  state is sent.

## Configuration and budget

| Setting | Default | |
|---|---|---|
| `FAST_REASONING_PROVIDER` | `disabled` | `jev` enables shadow triage; a key alone selects nothing |
| `TYPESAFE_API_KEY` | empty | required when `jev`; `SecretStr`, never logged or persisted |
| `JEV_MODEL` | `jev-1.13.0` | pinned version only |
| `JEV_BASE_URL` | `https://api.typesafe.ai` | |
| `JEV_TIMEOUT_SECONDS` | 10 | one attempt |
| `JEV_MAX_ASSESSMENTS_PER_RUN` | 20 | a full discovery: 10 pools × 2 chains |
| `JEV_MAX_ASSESSMENTS_PER_DAY` | 1920 | that for every 15-minute run: 20 × 96 |

Dimensioning (measured discovery 2026-09-28: 19–20 valid pools per run, both
chains):

| | |
|---|---|
| Expected new candidates per day | ≈ 1,900 (≤ 20 × 96 runs) |
| JEV calls for full coverage | ≈ 1,900/day, never more than one per stream |
| Configured daily cap | 1,920 (full coverage, still a hard bound) |
| Provider limits (official) | 1,200 requests/min, 250k tokens/s — a run's 20 calls are far below |
| Price (official, models page) | $0.042 per million **input** tokens; output tokens free |
| Input size per call | ≈ 620 tokens estimated from the request body (2,485 chars); not yet measured on a live call — budgeted at ≤ 1,000 |
| Estimated cost | ≈ $0.05–0.08/day, ≈ $1.50–2.40/month at the full cap |

The cost is an estimate from the official price and an estimated token count;
the first live `usage.input_tokens` replaces the estimate. The budget is its
own (`discovery_fast_assessments` counted per UTC day under an advisory
lock), never mixed with the ORBIT budget.

## Input (`schema_version` 1)

Only what was observed by the assessment instant, and no watch, ORBIT or
case state: chain, venue, stream age (since the stream's first observation),
observation age, price / liquidity / volume (status, decimal value, magnitude
band computed in code, volume window), `volume_to_liquidity` (computed), the
stream's previous observation if there is one (minutes before, percentage
changes computed in code) and the list of data gaps. No symbol, name or
address; no market-cap or liquidity floor; bands describe, they do not gate.
The input is stored with its SHA-256 digest (`input_digest`).

## Questions `jev-scout-v1`

`data_quality` (choice: complete/partial/insufficient), `organic_activity`
(score, 4 levels), `suspicious_activity` (noul), `anomaly_signal` (noul),
`momentum_quality` (score, 4 levels; level 0 when there is no earlier
observation), `continuation_signal` (noul). None asks whether to buy, sell or
trade. A new question set is a new version and a new row per stream.

## Table `discovery_fast_assessments` (migration 0016)

**Identity: the discovered stream, not a watch.** There is no separate
stream table; the existing stream identity — `(provider, chain, network,
pair_id, is_fixture)`, the same unique key `discovery_watches` and
`discovery_stream_declines` use — is stored as `market_provider, chain,
network, pair_id, is_fixture`, plus `snapshot_id` (FK, NOT NULL) to the exact
observation shown. There is no `watch_id`: a watch, when there is one, is
found by the same stream key, so no second identity can drift.

**Uniqueness:** `UNIQUE (market_provider, chain, network, pair_id,
is_fixture, question_version)` — one JEV-0 per stream and question set,
whether or not a watch was ever opened, so a pool seen again in a later run is
never asked twice.

Columns for provenance (`provider`, requested `model`, answering
`model_version`, `question_version`, `input_schema_version`, `input_digest`),
status and timing (`reserved_at`, `assessed_at`, `utc_day`, `latency_ms`,
tokens) and failures (`failure_category`, `failure_reason_code`, sanitised to
a code or `UNCLASSIFIED`). The typed input and answers are versioned JSONB,
validated by pydantic on the way in.

Reserved (`PENDING`) and committed before the call, settled exactly once to
`COMPLETED` or `FAILED`; a settled row never changes. Database checks enforce
"completed ⇔ answers", "failed ⇔ category", "pending ⇔ not settled".

## Calibration design

**Population.** Every assessed candidate, watched or declined, so later we
can ask not only "did JEV agree with Codex?" but "did the neutral allocation
miss a later winner?".

**Linking.** Everything joins on the stream key in PostgreSQL: the JEV-0 row;
the watch, if the stream became one; the first Codex ORBIT review *after* the
JEV row (watches only); future observations of the stream; and later outcome
labels. A candidate **without** a watch stays linkable: its stream key and its
recorded observation carry the pool locator, so an outcome labeler can read
the pool's OHLCV history from the provider for any horizon even if discovery
never re-observed it.

**Metrics.** Against Codex (watched candidates only): **`RECALL_INTERESTING`**
— of 131 Codex reviews so far, 130 were `NOT_INTERESTING` and 1
`INTERESTING`, so accuracy would be meaningless. Against objective outcomes
(all candidates, which is what keeps Codex from being treated as absolute
ground truth): **`RECALL_TOP_RETURN`** for a top-return quantile still to be
defined, plus max return, max drawdown, survival, liquidity persistence and
volume persistence. No threshold is fixed yet. Also: false-negative rate,
precision, coverage, Brier score and ECE on the noul probabilities.

**Random label sample.** Codex results are FIFO-biased, not random.
`src/scout/calibration.py::calibration_sample(candidates, seed, per_chain)`
ranks candidates (by fast-assessment id, so declined ones are included) by
`sha256(seed:id)` and takes the lowest ranks per chain:
reproducible, chain-stratified, and blind to JEV (it is only given ids and
chains). It is a design helper; using it to choose Codex reviews would be a
queue-policy change and is a separate decision.

**Outcome labels (designed, not created).** A later table
`discovery_stream_outcomes` with one row per stream key and horizon — not
per watch, so declined candidates are labelled too:
`horizon` ∈ {15m, 1h, 3h, 6h, 24h, 72h}; `computed_at`; `return_pct`,
`max_return_pct`, `max_drawdown_pct` (from recorded observations/OHLCV only);
`liquidity_survived`, `volume_persisted`, `pool_survived` (booleans with an
explicit UNKNOWN); `became_watch`, `vector_mature` and `trade_case_id`
(nullable, read from the existing tables); `source`, `schema_version`. Unique
`(stream key, horizon, schema_version)`, index on `(horizon, computed_at)`. A labeler would read only
data observed after the assessment and write this table; it trades nothing.

## Read-only analysis

`GET /api/scout/shadow/summary` (`ShadowReadService`, bounded to the latest
5,000 settled rows) counts **every** fast assessment, watched or not: totals
by status and answering model version, failure codes, latency p50/p95,
per-question signal distributions, counts by chain, `with_watch` /
`without_watch`, and mean signals grouped by the first later Codex
classification (watched candidates only). Outcomes:
`NOT_YET_LABELLED` until the outcome table exists. The same questions in SQL:

```sql
-- success / failure and latency
SELECT status, failure_reason_code, count(*),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50_ms
FROM discovery_fast_assessments GROUP BY 1, 2;

-- watched versus declined candidates
SELECT (w.id IS NOT NULL) AS became_watch, count(*)
FROM discovery_fast_assessments f
LEFT JOIN discovery_watches w
  ON (w.provider, w.chain, w.network, w.pair_id, w.is_fixture)
   = (f.market_provider, f.chain, f.network, f.pair_id, f.is_fixture)
GROUP BY 1;

-- JEV 0h signal versus the first later Codex classification
SELECT o.classification,
       avg((f.answers -> 'suspicious_activity' ->> 'noul')::float) AS mean_suspicious,
       count(*)
FROM discovery_fast_assessments f
JOIN discovery_watches w
  ON (w.provider, w.chain, w.network, w.pair_id, w.is_fixture)
   = (f.market_provider, f.chain, f.network, f.pair_id, f.is_fixture)
JOIN LATERAL (
  SELECT a.classification FROM discovery_watch_assessments a
  WHERE a.watch_id = w.id AND a.status = 'COMPLETED'
    AND a.assessed_at > f.reserved_at
  ORDER BY a.assessed_at LIMIT 1
) o ON true
WHERE f.status = 'COMPLETED'
GROUP BY 1;
```

## Tests that pin the shadow invariant

- `test_every_new_candidate_is_assessed_not_only_the_watches`: 20 discovered
  (10 per chain), limit 10 → 20 observations, 10 watches (5 + 5), 10 declined,
  20 JEV-0 assessments covering both chains, declined streams included.
- `test_the_next_run_asks_no_one_twice_and_opens_no_declined_watch`: the same
  pools again → 0 watches, 0 declines, 0 JEV calls.
- `test_jev_off_on_or_failing_changes_no_decision_input`: identical runs with
  JEV off, on, and failing from the 11th call; every decision input (watch
  status, schedules, checkpoint index, VECTOR verdict, promoted case,
  declines, the ORBIT calls made) is identical, and TradeCase, risk-request,
  order, fill, position and evidence counts stay zero.
