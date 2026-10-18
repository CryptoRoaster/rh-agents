# Early discovery: shadow run B results and configuration recommendation

Issue #77. Evaluation of the 24-hour discovery shadow run `20261014T184926Z`
(`SHADOW_DISCOVERY_B`), read offline from its own SQLite store. No new
measurement, no runtime change. Nothing here changes a setting; it records the
evidence for a later, separately approved configuration change.

## The run

| Item | Value |
|---|---|
| Source | GeckoTerminal `robinhood/new_pools`, page 1 (20 pools), `include=base_token,quote_token,dex` |
| Window | 2026-10-14 18:49 UTC to 2026-10-15 18:46 UTC (23.95 h), end reason `MAX_RUNTIME` |
| Polls | 288 of 288 successful, 288 requests, no retry |
| Interval | median 300.4 s, min 300.0 s, max 301.0 s, none above 330 s |
| Errors / rate limits | none / no HTTP 429 |
| Latency | median 111 ms, p95 258 ms, max 8.7 s |

## Discovery facts

| Metric | Value |
|---|---|
| Unique pools seen | 3 761 |
| Created inside the window | 3 741 |
| New pools per hour | 156 on average; 93 (03 UTC) to 222 (17 UTC) |
| New pools per poll | median 13 |
| Overlap with the previous poll | median 7 of 20 |
| Time span of one page | median 437.5 s |
| Polls with no overlap (possible gap) | 55 of 287 (19.2 %) |
| Gap time / estimated missed pools | 353 s / about 43 (≈ 1.1 %) |
| Discovery delay (creation → first seen) | p50 243 s, p90 429 s, p95 500 s, max 1 100 s |

The estimate of missed pools extrapolates each gap with that page's own
creation rate. It is an estimate, not a count: a poll with no overlap proves
only that the page boundary was not observed.

### Coverage of the current profile against the alternatives

Simulated on the same data (share of the 3 741 pools created in the window):

| Profile | Coverage |
|---|---|
| every 5 min, all 20 | 100 % of what the shadow saw (≈ 98.9 % of all, after the gap estimate) |
| every 5 min, first 10 | 68.8 % |
| every 10 min, all 20 | 71.1 % |
| every 15 min, all 20 | 50.5 % |
| **every 15 min, first 10 (current: 15-min scout, `early_scout_max_discovery_pools=10`)** | **25.4 %** |

### Pagination need

One page covers about 7.3 minutes at the average rate and 5.4 minutes in the
busiest hour. Pages needed per poll: 5 min → 1 (average and peak); 10 min → 2;
15 min → 2 on average and 3 at peak. At a 5-minute interval a single page is
enough on average, but almost one poll in five showed no overlap, so a second
page is needed exactly when the first one does not reach the previous poll.

### Market mix

Pools created in the window, by pool locator:

| | Pools | Share |
|---|---|---|
| 32-byte pool id (V4-style) | 2 050 | 54.8 % |
| 20-byte pool address (V2/V3-style) | 1 691 | 45.2 % |

V4-style by venue: `uniswap-v4-robinhood` 1 891, `bankr-robinhood` 81,
`uniswap-pools-trade` 38, `clanker-robinhood` 23, `pons-v2-dex` 13,
`pancakeswap-infinity-clmm-robinhood` 3, `o1-launchpad-robinhood` 1. The
largest 20-byte venue is `pons-v2` (1 282 of all pools seen), then
`uniswap-v2-robinhood` and `uniswap-v3-robinhood`.

Maximum reported reserve while observed: 1 018 pools reached ≥ 10 000 USD
(743 of them V4-style) and 172 reached ≥ 100 000 USD (8 V4-style). GeckoTerminal
`reserve_in_usd` values the token side of a single-sided launch position, so it
overstates executable liquidity for young V4 launches; the two largest V4
readings (14.8 M and 7.4 M USD) are of that kind. The figure is a coarse
prefilter only; ANCHOR remains the measure of liquidity.

## Provider cost

288 requests per day for page 1 every 5 minutes; with a second page on every
non-overlapping poll, about 345 per day. The public GeckoTerminal limit
(30 requests per minute) is not approached, and the run saw no 429 and no
error. Discovery is not the cost driver: every pool passed on costs ATLAS
reads (holder provider and the V4 census over RPC) and ORBIT reviews, which are
bounded separately (`early_scout_max_orbit_reviews_per_day`, census
`max_requests`).

## Recommendation (for a later, separately approved change)

1. **Poll `new_pools` every 5 minutes**, not 15. The current profile sees about
   a quarter of new pools; 5 minutes with the whole page sees essentially all
   of them, at a delay of 4 minutes (p50) and 8.3 minutes (p95).
2. **Process the whole page** (`early_scout_max_discovery_pools=20`). At 5
   minutes the first ten cover only 69 %.
3. **Fetch page 2 only when page 1 has no overlap with the previous poll**, and
   record such a poll as a possible gap. That removes the estimated ≈ 1 % loss
   at about 20 % more requests; a fixed second page every time is not needed.
4. **Prefilter before any paid or RPC-heavy step**: only 32-byte V4 pool ids can
   reach `LP_CONTROL=PASS` today (20-byte pools are `UNSUPPORTED`, issue #92),
   so 20-byte pools should be watched at most, never sent to ATLAS for an
   entry. This halves the downstream load.
5. Keep the existing per-run and per-day caps; size the ORBIT and ATLAS budgets
   to the V4 share (≈ 2 000 V4 pools a day, of which a few hundred reach a
   10 000 USD reserve reading at some point).

## What this means for early entries today

Discovery is not the bottleneck. With the LP-control rules on main and in
review, a young V4 pool passes only when every position in its own pool is
verified permanently locked (#97) and the PoolManager holds nothing beyond
those positions' principal (#96). Every traded pool leaves swap fees and
rounding in the PoolManager, so in practice no traded V4 pool passes until the
remainder can be attributed exactly (complete pool discovery over the
PoolManager's history, #94, and fee credit in the economic figure). A changed
discovery configuration would therefore raise the number of pools reviewed, not
the number of entries.

## Uncertainty

- One 24-hour window on one weekday; the hourly rate varied by a factor of 2.4.
- Gaps are inferred from missing overlap, and missed pools are extrapolated.
- Discovery delay is measured against GeckoTerminal's `pool_created_at`, i.e.
  includes the provider's own indexing delay.
- Reserve figures are the provider's and unsuitable as a liquidity measure for
  single-sided V4 launches.

## Reproduction

Read-only, no network:

```
cd ~/.rh-agents-shadow/discovery-b-20261014/
/Volumes/Coding/rh-agents/backend/.venv/bin/python -I analyze_shadow.py 20261014T184926Z
```

The interval, latency, coverage-by-profile, pagination and market-mix figures
above come from a supplementary read-only query of the same `shadow.sqlite`
(polls, sightings, `pool_created_at`, `reserve_usd`, pool locator length).
The store was left unchanged.
