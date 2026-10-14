# Early pipeline readiness: holder data and discovery coverage (Issue #77)

This note follows the early-discovery acceptance review of 2026-10-14. It covers the two parts that were analysed and sized, not built:

- whether the holder rule can be met for young Robinhood tokens;
- how much of Robinhood's new-pool stream the scout actually sees.

The pipeline fixes from the same review are in the code and in [the strategy notes](pre-vector-early-entry.md):

- the two-pass contract, replayed;
- the funding-source preflight;
- named ATLAS evidence states.

Nothing here changes a threshold or the runtime.

## Holder concentration for young Robinhood tokens

SENTINEL requires two things:

- holder metrics;
- a top-10 fraction of at most 0.35 (`risk/engine.py`). The comparison is `>`, so exactly 0.35 passes.

UNKNOWN fails closed. The tests in `tests/test_risk.py`, `tests/atlas/v4/*` and `tests/riskdata/test_readiness.py` show this, and all of them pass.

**What exists**

- **Blockscout holder source** (`atlas_rh_holder_provider=blockscout`). It makes three requests: token, indexer head, and 50 holder rows per page with `atlas_holder_max_pages=1` by default. The fractions are measured against on-chain `totalSupply`. Only the zero address is reconciled back into the ranking.
- **V4 pool control** (`atlas_v4_pool_control_enabled`). It is required for `BYTES32_POOL_ID` markets, because a V4 pool's reserves sit in the PoolManager. Without it, a V4 BUY has unknown concentration and is refused.
  - The census replays `Initialize`/`ModifyLiquidity` from the creation block.
  - It credits each position to its controller.
  - It is bounded to 240 RPC requests and 30 s.
- **Position control.** An LP NFT owned by a plain wallet counts for that wallet. The official Uniswap FeeSplitter counts as locked. Any other contract custody, a third-party launchpad locker for example, is unresolved and therefore UNKNOWN.

**Why a legitimate PASS is unlikely for a launchpad V4 token** (read from the code, not measured):

- The LP NFT of a launchpad pool is held either by a launchpad contract or by an EOA:
  - launchpad contract → unresolved → UNKNOWN;
  - EOA → that wallet holds most of the supply → over 35 %.
- Pre-allocations, vesting and bonding-curve contracts count as ordinary holders.
- SENTINEL bounds the holder read to 30 s, measured from Blockscout's indexer-head time. The build order is holders, origin, census of up to 30 s, then funding. The read can therefore be stale before the risk request even starts. In that case the run's source refresh repeats the whole ATLAS read.
- Data gap in the code: a holder list that is only partly indexed (rows summing below supply) is computed, not refused. It could understate concentration. This is recorded here, not changed.

**Status: `HOLDER_PROBE_REQUIRED`.** Whether any young Robinhood token can pass is a live question the fixtures cannot answer.

### Proposed probe (needs its own approval; not run)

Read-only. The existing classes are used, so nothing new talks to a provider.

| Item | Bound |
|---|---|
| Tokens | 5–10 Robinhood tokens under 6 h old, from recorded scout or GeckoTerminal data. At least 3 `BYTES32_POOL_ID` launchpad pools and 1–2 V2/V3 pools as controls. |
| Per token | `BlockscoutHolderSource.holder_facts` (3 requests), `BlockscoutContractOriginSource.origin_facts` (1), `RpcTokenContractSource` chain snapshot, contract facts and zero-address balance (about 5 RPC calls), `V4PoolCensus.census` with the default bounds (240 RPC calls or fewer); then, in memory only, `pool_control` and `entry_concentration` |
| Caps | Blockscout 4 per token and 40 in total. RPC 250 per token and 2,500 in total. Sequential, one retry at most, abort after 10 minutes. |
| Secrets | The key is read from the environment only. No key, header or full URL is printed. |
| Output | The scratchpad only; no database write |

Recorded per token:

- token age, Blockscout status, `holders_count`, row count and completeness;
- sum of rows against `totalSupply`;
- indexer lag against the RPC head;
- zero-address balance, raw top 10, and the PoolManager's share;
- census status, gap, request count and time;
- positions with their control state and owner code hash;
- unattributed fraction, economic top 10 or floor;
- the SENTINEL reason codes it would produce;
- the elapsed time from holder observation to the end of the read, against 30 s.

## Discovery coverage

| Fact | Value | Basis |
|---|---|---|
| New Robinhood pools | about **35 per 15 min (2.3/min)** | **one** read-only `new_pools` page on 2026-10-14: 20 pools spanning 8.6 min. A single sample, not a rate. |
| One `new_pools` page | 20 pools, so about 8.6 min of Robinhood launches at that rate | same sample |
| Scout today | every 15 min, page 1 only, the newest `EARLY_SCOUT_MAX_DISCOVERY_POOLS=10` (max 20) per chain, at most `EARLY_SCOUT_MAX_NEW_WATCHES_PER_RUN=10` new watches per run | `scout/service.py`, `core/config.py`, launchd `StartInterval` 900 |
| Observed | 4,642 watches, 3,482 `NOT_OPENED_AS_WATCH_DUE_TO_WATCH_LIMIT`; nearly every run found 10 of 10 pools new | runtime database, 2026-09-27 to 2026-10-02 |
| Scout run time | about 57 s average, p95 81 s, max 282 s; about 5.6 provider requests per run | runtime `scout_runs` |
| Provider limit | GeckoTerminal's free API is documented publicly at about 30 calls per minute | provider documentation; **not verified here** |
| Pagination | `new_pools` takes `page`; the adapter asks for page 1 only | `markets/geckoterminal/adapter.py` |

The runtime is configured for Robinhood only (`MARKET_CHAINS=robinhood`), so the whole watch budget can go to that chain. The rates below are derived from the single sample above. They are estimates, not measured capture rates.

| Option | Requests for discovery | Estimated share of new pools seen | Delay at discovery | Notes |
|---|---|---|---|---|
| A. Today (15 min, 10 pools) | 4/h, 96/day | about 10/35, so **under 30 %** | 0–4 min for those seen | Most launches are never seen |
| B. 5 min, page 1, 20 pools, 20 watches per run | 12/h, 288/day | about **100 %** while launches stay under about 4/min | 0–5 min, about 2.5 average | Runs take about 1 min, so a 5-min interval fits |
| C. 15 min, 2 pages, 40 pools | 8/h, 192/day | about 100 % at the sampled rate | 0–15 min, about 7.5 average | Same coverage, but too late for a strategy that lives on the first minutes |
| D. 5 min, 2 pages | 24/h, 576/day | headroom for bursts over 4/min | as B | Doubles discovery requests for burst cover only |

Every option stays far below the documented limit. Including refreshes and history (about 5.6 requests per run), B is about 70 requests per hour.

**What grows with B**

- **Rows:** about 3,400 Robinhood watches and observations per day instead of about 440. That is modest; observations are small JSON rows.
- **ORBIT:** none, because the budget is capped (`EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_DAY=96`). It is already spent long before the end of a day, and 89 % of first reviews are skipped as stale.
  - The scout's ORBIT reviews do not decide early candidacy. Each early case gets its own ORBIT review.
  - Raising the ORBIT budget would add model cost without moving early entries.
- **Early cases:** the early intake opens at most one case per cycle (`max_cases_per_cycle=1`). Seeing more launches does not open more cases. It gives the intake younger and better candidates to choose from.

**Recommendation, not activated**

1. Measure first. Run one day with option B on a non-runtime database, or as a shadow, and compare seen launches with the provider's own listing.
2. If it holds, use B:
   - scout interval 300 s;
   - `EARLY_SCOUT_MAX_DISCOVERY_POOLS=20`;
   - `EARLY_SCOUT_MAX_NEW_WATCHES_PER_RUN=20`.
3. Keep pagination at one page unless bursts above about 4 launches per minute show up.
4. Leave the ORBIT budget unchanged.

Any of this is a separate, approved change.
