# JEV-0: shadow triage of new scout watches

**Shadow only.** JEV writes evidence for later calibration. Nothing reads it for
a decision: not watch creation, the ORBIT queue or selection, the ORBIT budget,
checkpoints, the watch lifecycle (WATCHING / DORMANT / PROMOTABLE), VECTOR,
TradeCases, SENTINEL/risk, orders, fills, positions or execution.

## Where it runs

```
new pool → recorded observation (PostgreSQL) → watch opened (≤ 10/run)
                                  │
   ORBIT (Codex) reviews, history checks — unchanged, run first
                                  │
   JEV shadow: one typed assessment per watch opened in this run
                                  │
                 discovery_watch_fast_assessments (PostgreSQL)
```

ORBIT/Codex → VECTOR → SENTINEL/risk continue exactly as before. JEV and Codex
are separate evidence producers; they meet only in read-only analysis.

JEV runs **last** in a scout run, after every ORBIT review and history check,
so it cannot change what the run decided, nor its timing. Only watches opened
in that run are assessed; older watches are never backfilled. A failure,
timeout, rate limit or a store that cannot be written stays in the shadow
tally: it is not a run error, triggers no retry and no Codex call.

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

## Configuration

| Setting | Default | |
|---|---|---|
| `FAST_REASONING_PROVIDER` | `disabled` | `jev` enables shadow triage; a key alone selects nothing |
| `TYPESAFE_API_KEY` | empty | required when `jev`; `SecretStr`, never logged or persisted |
| `JEV_MODEL` | `jev-1.13.0` | pinned version only |
| `JEV_BASE_URL` | `https://api.typesafe.ai` | |
| `JEV_TIMEOUT_SECONDS` | 10 | one attempt |
| `JEV_MAX_ASSESSMENTS_PER_RUN` | 10 | = the per-run watch limit |
| `JEV_MAX_ASSESSMENTS_PER_DAY` | 960 | = the most watches the limit can open in a UTC day (10 × 96) |

The budget is its own (`discovery_watch_fast_assessments` counted per UTC day
under an advisory lock), never mixed with the ORBIT budget. Cost at the cap:
≈ 960 × ~1k input tokens ≈ $0.04/day.

## Input (`schema_version` 1)

Only what was observed by the assessment instant: chain, venue, watch age,
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
trade. A new question set is a new version and a new row per watch.

## Table `discovery_watch_fast_assessments` (migration 0016)

Columns for identity and provenance (`watch_id` FK, `snapshot_id` FK to the
observation, provider, requested `model`, answering `model_version`,
`question_version`, `input_schema_version`, `input_digest`), status and
timing (`reserved_at`, `assessed_at`, `utc_day`, `latency_ms`, tokens) and
failures (`failure_category`, `failure_reason_code`, sanitised to a code or
`UNCLASSIFIED`). The typed input and typed answers are versioned JSONB
documents, validated by pydantic on the way in, because the question set is
expected to evolve by version.

Reserved (`PENDING`) and committed before the call, settled exactly once to
`COMPLETED` or `FAILED`; a settled row never changes. Database checks enforce
"completed ⇔ answers", "failed ⇔ category", "pending ⇔ not settled", and
`UNIQUE (watch_id, question_version)` prevents a second assessment.

## Calibration design

**Linking.** Everything joins on `watch_id` in PostgreSQL: the JEV 0h row, the
first Codex ORBIT review *after* it, and (later) outcome labels.

**Primary metric: `RECALL_INTERESTING`.** Of 131 Codex reviews so far, 130
were `NOT_INTERESTING` and 1 `INTERESTING`, so accuracy would be meaningless.
Secondary: false-negative rate, precision, coverage, Brier score and ECE on
the noul probabilities (Jev returns calibrated probabilities).

**Random label sample.** Codex results are FIFO-biased, not random.
`src/scout/calibration.py::calibration_sample(watches, seed, per_chain)` ranks
watches by `sha256(seed:watch_id)` and takes the lowest ranks per chain:
reproducible, chain-stratified, and blind to JEV (it is only given ids and
chains). It is a design helper; using it to choose Codex reviews would be a
queue-policy change and is a separate decision.

**Outcome labels (designed, not created).** A later table
`discovery_watch_outcomes` with one row per `(watch_id, horizon)`:
`horizon` ∈ {15m, 1h, 3h, 6h, 24h, 72h}; `computed_at`; `return_pct`,
`max_return_pct`, `max_drawdown_pct` (from recorded observations/OHLCV only);
`liquidity_survived`, `volume_persisted`, `pool_survived` (booleans with an
explicit UNKNOWN); `vector_mature` and `trade_case_id` (nullable, read from the
existing tables); `source`, `schema_version`. Unique `(watch_id, horizon,
schema_version)`, index on `(horizon, computed_at)`. A labeler would read only
data observed after the assessment and write this table; it trades nothing.

## Read-only analysis

`GET /api/scout/shadow/summary` (`ShadowReadService`, bounded to the latest
5,000 settled rows): totals by status and answering model version, failure
codes, latency p50/p95, per-question signal distributions, counts by chain,
and mean signals grouped by the first later Codex classification. Outcomes:
`NOT_YET_LABELLED` until the outcome table exists. The same questions in SQL:

```sql
-- success / failure and latency
SELECT status, failure_reason_code, count(*),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50_ms
FROM discovery_watch_fast_assessments GROUP BY 1, 2;

-- JEV 0h signal versus the first later Codex classification
SELECT o.classification,
       avg((f.answers -> 'suspicious_activity' ->> 'noul')::float) AS mean_suspicious,
       count(*)
FROM discovery_watch_fast_assessments f
JOIN LATERAL (
  SELECT a.classification FROM discovery_watch_assessments a
  WHERE a.watch_id = f.watch_id AND a.status = 'COMPLETED'
    AND a.assessed_at > f.reserved_at
  ORDER BY a.assessed_at LIMIT 1
) o ON true
WHERE f.status = 'COMPLETED'
GROUP BY 1;
```

## Tests that pin the shadow invariant

`tests/scout/test_shadow.py::test_jev_on_or_off_changes_no_decision_input`
runs the same scout runs with the fast provider off and on and compares every
decision input field by field (watch status, `next_orbit_review_at`,
checkpoint index, history schedule, VECTOR verdict, promoted case, the ORBIT
calls made) and the TradeCase/risk-request/order/fill/position/evidence
counts, which stay zero.
