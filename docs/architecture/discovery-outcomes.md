# Discovery outcomes: objective labels for every candidate

**Labels only.** Nothing reads them for a decision: no watch, ORBIT, JEV,
VECTOR, risk or execution behaviour changes. They exist to measure JEV, ORBIT
and later entry strategies against what markets actually did, not against each
other.

## What is measured

For every discovery stream — watched, or declined by the watch limit — over the
horizons **15m, 1h, 3h, 6h, 12h, 24h, 48h, 72h** after first sight:

| Label | Definition |
|---|---|
| `return_pct` | close of the last bar opened inside the window vs. the reference price |
| `max_return_pct` | highest high inside the window vs. the reference price (upside) |
| `max_drawdown_pct` | lowest low inside the window vs. the reference price |
| `survived` | a bar with volume > 0 in the last quarter of the window (at least one bar) |
| `volume_usd`, `second_half_volume_share` | traded volume in the window, and the share in its second half (volume persistence) |
| liquidity persistence | per stream: liquidity at the reference observation and re-observed at sampling time |

The reference is the stream's **first recorded observation** (instant and USD
price — what JEV and ORBIT were shown). A horizon is labelled only when
recorded OHLCV reads covered the whole window at a resolution no coarser than
the horizon; otherwise it is `MISSING` with `HISTORY_NOT_COVERED`,
`RESOLUTION_TOO_COARSE` or `REFERENCE_PRICE_UNAVAILABLE`. GeckoTerminal omits
intervals without trades, so a covered window without bars is labelled as a
fact: no trades, not survived. Labels are exact to one bar (15 minutes).

**Out-of-range outcomes.** A percent outcome is a label only if it fits the
canonical `NUMERIC(24, 6)` — eighteen integer digits. That is a technical
bound, not a market rule: a 100x (9,900 %), a 1000x (99,900 %) and even a
10^9-fold move (~10^11 %) are ordinary labels. A horizon whose return, maximum
return or maximum drawdown does not fit — in practice a provider bar off by
many orders of magnitude, or an absurdly small reference price — is recorded as
`MISSING` with `OUTCOME_PERCENT_OUT_OF_RANGE`: no value is clamped, rounded to a
bound or replaced by a sentinel, the percent, survival and volume fields are
null, and the measurement's provenance (timeframe, aggregate, bars used) is
kept. Other horizons of the same stream are unaffected, the stream is sampled
once like any other, and the read view lists the reason without letting it
reach any median or top-outcome comparison.

**Isolation.** Each stream is stored in its own transaction. A value the
database still refuses as out of range (SQLSTATE 22003) rolls back that stream
alone and is counted as `OUTCOME_VALUE_NOT_STORABLE`; the next stream is
stored. Every other database error — a lost connection, a broken transaction —
still ends the step as `OUTCOME_STORE_UNAVAILABLE`. Origin: on 2026-10-01 a BSC
stream with a 9.3e-9 USD reference and a 195,753,160 USD bar open produced a
2.1e18 % maximum return, and the overflow stopped all outcome sampling.

## Data model (migration 0017, PostgreSQL)

- `market_ohlcv_fetches` — one row per OHLCV read: stream key, timeframe,
  the **window it covered**, source (`SCOUT_VECTOR_HISTORY` or
  `OUTCOME_SAMPLER`), bars returned.
- `market_ohlcv_bars` — closed bars per stream, `UNIQUE (stream, timeframe,
  aggregate, opened_at)`, written once; unconstrained `NUMERIC` so prices far
  below 1e-9 USD stay exact.
- `discovery_outcome_samples` — one row per stream and schema version: the
  reference observation (FK), status `COMPLETE`/`PARTIAL`/`UNAVAILABLE`,
  `history_source` `REUSED`/`FETCHED`/`NONE`, provider requests spent, and
  liquidity at reference and at sampling.
- `discovery_stream_outcomes` — one row per stream, horizon and schema version
  (`UNIQUE`), the labels above; checks enforce "missing ⇔ reason" and a
  positive horizon. The three percent columns are `NUMERIC(24, 6)` since
  migration 0022 (0017 created them unbounded while the models declared
  `NUMERIC(24, 6)`).

Everything is keyed by the existing stream identity (provider, chain, network,
pair, fixture flag), so watches, declines, JEV-0 rows and outcomes join
directly, and candidates without a watch are labelled like watched ones.

## Sampling and request budget

- **When:** a stream is sampled once, when every horizon has closed (first seen
  + 72 h + one bar) and while one read can still reach first sight (≤ 240 h;
  1,000 × 15-minute bars).
- **How much:** one 15-minute OHLCV read per stream labels all eight horizons.
  Before reading, stored bars are tried: a stream whose window is already
  covered is labelled without a request (`REUSED`).
- **Which first:** a keyed hash of the stream identity (`OUTCOME_SAMPLE_SEED`),
  never price, liquidity, JEV or ORBIT — the labelled set is a signal-blind
  random subset when the budget cannot cover everything.
- **Budget:** its own GeckoTerminal transport and cap, `OUTCOME_MAX_REQUESTS_PER_RUN`
  (default 10): one network lookup, one batched liquidity re-observation per
  chain, and the rest (7 with two chains) for OHLCV reads. It runs **last** in
  a scout run, after discovery, ORBIT refreshes, history checks and JEV, so it
  never displaces them. `OUTCOME_MAX_STREAMS_PER_RUN` (default 40) bounds reused
  labelling per run. A rate limit, a spent budget or an auth failure ends the
  run's provider work; transient errors leave a stream for a later run; errors
  that cannot change on retry record the stream as `UNAVAILABLE`.
- **Off by default:** `OUTCOME_SAMPLER_ENABLED=false`.

Expected coverage at the defaults: ≈ 20 new candidates per 15-minute run
(≈ 1,900/day) against 7 reads per run (≈ 670/day), i.e. **≈ 35 %** of candidates
labelled by fresh reads, plus reused labels. Requests per run rise from ~7 to
~17, within GeckoTerminal's documented public limit of 30/min. Raising
`OUTCOME_MAX_REQUESTS_PER_RUN` to ~30 would cover every candidate if the rate
limit allows; that is a separate, measured decision.

## Reuse with VECTOR history

The scout's VECTOR history check now records its hourly read (48 bars) in the
bar store. When those reads cover a stream's whole 72-hour window (for example
the 24h and 72h checkpoints together), the sampler labels the seven horizons
from 1h to 72h without a request; only the 15-minute horizon stays `MISSING`
(`RESOLUTION_TOO_COARSE`), which is not worth a read. The VECTOR check itself
is unchanged — same request, same verdict, same policy — and does not read
sampler bars (15-minute bars would need aggregation that VECTOR's policy does
not define).

## Read-only analysis

`GET /api/scout/outcomes/summary` (`OutcomeReadService`, bounded):
coverage (candidates, sampled, status, history source, missing reasons per
horizon), per-horizon medians of return, max return, drawdown and survival
rate, liquidity persistence, and at the 24h focus horizon: watched vs.
declined, by chain, by first ORBIT classification, and each JEV-0 signal's
mean among the top 10 % outcomes (by max return) vs. the rest, with
`recall_top_return_at_half` for yes/no signals. Descriptive only; the top
share and focus horizon are stated in the response and are not thresholds.
