# ADR: PostgreSQL is the durable source of truth

**Status:** accepted (2026-09-28). **Scope:** every durable RH-Agents state.

## Decision

1. **PostgreSQL is the one durable source of truth.** It stores market
   observations and streams, discovery watches and stream declines, scout runs,
   ORBIT assessments, JEV/fast shadow assessments of discovery candidates, budget reservations,
   TradeCases and their evidence, risk requests, orders, fills (`trades`),
   positions, and later outcome labels, model provenance and calibration data.
2. **SQLite is a test backend only.** Runtime `Settings` refuse any URL that
   is not `postgresql+asyncpg://`; SQLite engines are built directly by tests
   and never through configuration. PostgreSQL-only behaviour (advisory locks,
   JSONB, timestamptz, `ON CONFLICT`) is tested against PostgreSQL.
3. **No Convex.** Convex is not a database or persistence layer for
   RH-Agents. It is not present in the repository (no code, no dependency, no
   lockfile entry, no docs reference as of this ADR), so nothing had to be
   migrated.
4. **No second persistent truth:** no Redis as source of truth, no separate
   vector database, no separate time-series database. Caches may exist later
   only if they are rebuildable from PostgreSQL.
5. **Writers:** ORBIT writes PostgreSQL; JEV writes PostgreSQL; the scout, the
   PAPER runner, the worker runtime and the ledger write PostgreSQL through
   repositories in one process boundary (the backend).
6. **Readers:** the cockpit reads only through the backend API:
   `Frontend → Next.js GET proxy → FastAPI → repository/service → PostgreSQL`.
   The frontend has no database driver, no credentials and no Convex or other
   client SDK.

## Persistence map (audit 2026-09-28)

| Finding | Where | Class |
|---|---|---|
| PostgreSQL via SQLAlchemy async + asyncpg, Alembic 0001–0016 | `backend/src/data`, `backend/migrations` | PRODUCTION_SOURCE_OF_TRUTH |
| SQLite (`aiosqlite`, dev dependency) | test engines; dialect branches in `markets/recorder.py`, `runtime/store.py`, `scout/repository.py` | TEST_ONLY |
| `DATABASE_URL` | `core/config.py` (validated `postgresql+asyncpg`), `.env` | PRODUCTION_SOURCE_OF_TRUTH (config) |
| Browser `localStorage` (`rh-agents.layout.v1.*`) | `frontend/components/console/sortable.tsx` | CACHE_ONLY (UI layout preference, never trading state) |
| GeckoTerminal pass-scoped snapshot lookup | `markets/geckoterminal/adapter.py` | CACHE_ONLY (cleared on rediscovery) |
| Codex harness temp trees (isolated `CODEX_HOME`, catalog copy) | `evaluation/codex/prepared_run.py` | CACHE_ONLY (deleted on exit) |
| Scout log `~/Library/Logs/rh-agents/scout.log` | `ops/scout/run-scout.sh` | DOC_ONLY (operational log; the run row in `scout_runs` is the record) |
| Convex | – | NOT PRESENT |
| Redis | – | NOT PRESENT (the only hits are the word "rediscovery") |

## Schema principles

- Relational core fields are explicit, typed columns: ids, timestamps,
  provider, model, versions, status, `watch_id`, `input_digest`,
  `schema_version`, latency, `failure_category`, `failure_reason_code`.
- JSONB only for versioned, typed documents whose shape evolves by version:
  market snapshot payloads, a model's typed output, a JEV question set's
  answers and its typed input. Never a whole domain object as a blob to avoid
  columns.
- Integrity lives in the database wherever it can: foreign keys, unique
  constraints (e.g. one fast assessment per watch and question version, one
  decline per stream), check constraints (status sets, "completed ⇔ answered",
  "failed ⇔ reason named", "pending ⇔ not settled"), transactions, advisory
  locks for budgets, append-only evidence.

## Index audit (query paths that exist)

| Path | Index | Verdict |
|---|---|---|
| ORBIT due queue: `status`, `next_orbit_review_at` ≤ now, order by `next_orbit_review_at, first_seen_at, pair_id` | `ix_discovery_watches_orbit_due (status, next_orbit_review_at)` | sufficient |
| History due queue | `ix_discovery_watches_history_due (status, next_history_review_at)` | sufficient |
| Watch by stream | `uq_discovery_watch_stream (provider, chain, network, pair_id, is_fixture)` | sufficient |
| ORBIT assessments by watch/time | `ix_discovery_watch_assessments_watch_time (watch_id, assessed_at)` | sufficient |
| Observations by stream/time | `ix_observation_pair_time (provider, pair_id, observed_at, id)` (pair_id carries chain:network) | sufficient |
| Scout runs by start | `ix_scout_runs_started` | sufficient |
| ORBIT budget by day | `ix_scout_orbit_reservations_day` | sufficient |
| JEV assessments by stream, by day, by time | `uq_fast_assessment_stream_questions` (stream key + question version), `ix_fast_assessments_day`, `ix_fast_assessments_reserved` (0016) | added with the table |
| Decline lookup (bootstrap) | `uq_discovery_stream_decline` | added with the table |
| Outcome labels by stream/horizon | `uq_stream_outcome_horizon`, `ix_stream_outcomes_horizon` (0017) | added with the table |
| OHLCV bars / reads by stream | `uq_ohlcv_bar`, `ix_ohlcv_fetches_stream` (0017) | added with the table |

Watch item: the recovery bootstrap groups `market_observations` by stream
each run. At today's volume that is trivial; at tens of millions of rows it
would want a stream table or a bounded time window. Not changed now.

## Growth estimate and TimescaleDB

Measured on the local instance (2026-09-28): 535 observations in 6 h
(≈ 89/h, ≈ 2,100–2,300/day at 20 recorded pools plus up to 4 refreshes per
run); `market_observations` is 2.1 MB for 1,026 rows including indexes
(≈ 2.1 KB/row). The whole database is 14 MB.

| | Rows | Size (with indexes) |
|---|---|---|
| per day | ≈ 2,300 | ≈ 5 MB |
| per month | ≈ 70,000 | ≈ 150 MB |
| per year | ≈ 840,000 | ≈ 1.8 GB |

JEV shadow assessments add at most 1,920 rows/day (≈ 3–4 KB each, ≈ 2.5 GB/year).

**`PLAIN_POSTGRESQL_SUFFICIENT = YES`**, **`TIMESCALEDB_NOW = NO`.** Range
queries are per stream and short; there is no retention or compression
requirement yet. Revisit TimescaleDB only on a measured trigger:

- observation range queries on the stream index exceed ~100 ms p95, or
- `market_observations` passes ~50 M rows / ~50 GB, or
- an explicit retention/compression requirement appears (e.g. raw ticks).

## pgvector

**`PGVECTOR_NOW = NO`.** Possible future feature: *historical launch
similarity* — "which earlier launches looked like this one in their first
30/60/180 minutes?". If it is built, it belongs inside PostgreSQL via the
`pgvector` extension, next to the observations it is derived from. Not now: no
extension, no vector column, no embeddings, no embedding API calls.
