# Early discovery: persistent scout watches

Decision for issue #39: `EARLY_SCOUT_WATCH_PIPELINE`.

GeckoTerminal `new_pools` stays the discovery universe, and a young pool is
looked at from its first valid observation. It is not held back until it is 24
hours old. The full TradeCase pipeline still requires VECTOR-sufficient
history, so a young market is kept in a persistent **watch** outside the
TradeCase until it has that history.

```
new pool (valid identity; provider_identity rejections create nothing)
  → discovery watch (one per market stream)
  → early ORBIT at T+0, 1h, 3h, 6h, 12h, 24h (re-observed by exact pool locator)
  → from T+24h: structural VECTOR history check (24h, 48h, 72h)
      insufficient → keep watching; after 72h → DORMANT (kept, no more traffic)
      sufficient   → PROMOTABLE
  → later, separate full PAPER run: fresh re-observation → COMMANDER intake
  → full TradeCase: ORBIT + VECTOR + PULSE → ANCHOR → SENTINEL → PAPER
```

## Scout watch lifecycle

| Status | Meaning |
|---|---|
| `WATCHING` | On the schedule. |
| `PROMOTABLE` | VECTOR-sufficient history at T+24h or later. A candidate for a later full run, not an approval. |
| `DORMANT` | No sufficient history by T+72h. Kept with its history. It causes no further provider or model traffic in V1. |
| `RETIRED` | The provider contradicted the stored market identity, so the watch fails closed. |

The ORBIT classification is not a status. `INTERESTING`, `NOT_INTERESTING` and
`INSUFFICIENT_DATA` all leave a watch `WATCHING`. A weak reading at T+0 must not
lose a market that becomes interesting at T+6h, because that development is
what the timeline is meant to record.

Watch identity is the market stream: provider, chain, network, pair and
fixture flag. `first_seen_at` is the source instant of the first observation
this system recorded. It is not the pool's creation time, and it never moves.

## Review checkpoints (`EARLY_SCOUT_V1`)

- ORBIT: 0h, 1h, 3h, 6h, 12h, 24h after `first_seen_at`.
- History: 24h, 48h, 72h after `first_seen_at`.
- No classification changes a checkpoint. An `INTERESTING` watch gets no extra
  calls and a `NOT_INTERESTING` one gets no fewer.
- A late run takes **one** review on the current fresh reading and then moves
  to the next future checkpoint. Missed checkpoints are not replayed. Each
  watch gets at most one ORBIT call per run.
- A checkpoint counts as spent when the review starts. A failed review is
  stored as `FAILED` with its typed reason, and the watch waits for its next
  checkpoint, so a provider outage cannot turn into repeated paid retries.
- When the review budget cuts the due list, due watches are ordered by due
  time, then `first_seen_at`, then pair. They are never ordered by liquidity,
  volume, price or classification.

## Maturity vs VECTOR sufficiency

Maturity requires both **age** (at least 24h since first seen) and
**structure**. Structure is checked with one OHLCV read through
`GeckoTerminalOhlcvSource` and VECTOR's own `assess(...)` under the unchanged
`VECTOR_SETUP_V1`: hourly bars, 48 requested, at least 24 closed, at most 2h
old and at most 25% missing. The history check makes no model call, and the
scout adds no sufficiency rule of its own.

## Scout vs TradeCase

- The scout (`--scout-once`) never opens a TradeCase. It also never requests
  risk, sizes, fills, signs or broadcasts, and it works in `OBSERVE` as well
  as `PAPER`.
- Scout assessments (`discovery_watch_assessments`) are append-only discovery
  history. They do **not** authorise execution and are never copied into a
  case as `DISCOVERY_EVIDENCE`. A case formed from a PROMOTABLE watch runs its
  own ORBIT task on its own fresh input.
- ORBIT is shared, not duplicated. Both paths use `OrbitEvaluator`, with the
  same instructions, prompt version and hash, input document and digest,
  validator and output schema.

## Promotion lifecycle

With `EARLY_SCOUT_ENABLED=true`, the full PAPER run (`--once`) no longer opens
cases on arbitrary fresh new pools. Its intake reads only fresh markets behind
`PROMOTABLE` watches:

1. Before intake, eligible PROMOTABLE watches without a fresh reading are
   re-observed by exact pool locator. This is bounded by
   `EARLY_SCOUT_MAX_REFRESH_MARKETS_PER_RUN`, and no discovery scan happens.
2. The candidate source removes markets that COMMANDER would refuse (a live
   case, `RISK_REJECTED` or `EXECUTED`) **before** the candidate limit, using
   COMMANDER's own queries. `EXPIRED` and `CANCELLED` remain eligible for a
   successor case.
3. COMMANDER decides as before: one active case per market, the same
   generations and the same bars.

A watch that becomes PROMOTABLE in a scout run is never consumed in that same
run, because the scout cannot open cases.

With the scout disabled, intake behaves exactly as before.

## Neutral discovery

The scout applies no market-cap minimum or maximum. It does not rank pools by
liquidity, volume, trend or momentum, and it does not use a top-pool universe.
Small new tokens are watched like any other. ORBIT may rate low liquidity as
`NOT_INTERESTING`, and the watch stays in place.

## Not included: early execution

This pipeline grants no execution authority. Buying before VECTOR has 24
closed bars would need its own strategy, risk, sizing and PAPER-evaluation
policy (a separate follow-up issue). It is not built into discovery. SENTINEL
is never bypassed.

## Budgets (per scout run)

| Setting | Default |
|---|---|
| `EARLY_SCOUT_ENABLED` | `false` |
| `EARLY_SCOUT_MAX_DISCOVERY_POOLS` | 10 per chain |
| `EARLY_SCOUT_MAX_NEW_WATCHES_PER_RUN` | 10 |
| `EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_RUN` | 4 |
| `EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_DAY` | 96 |
| `EARLY_SCOUT_HISTORY_MAX_REQUESTS_PER_RUN` | 6 (own transport) |
| `EARLY_SCOUT_HISTORY_REQUEST_SPACING_SECONDS` | 6 |
| `EARLY_SCOUT_MAX_REFRESH_MARKETS_PER_RUN` | 4 |
| `EARLY_SCOUT_MAX_BOOTSTRAP_STREAMS` | 100 |

| `EARLY_SCOUT_DISCOVERY_PAGE2_ENABLED` | `false` |
| `EARLY_SCOUT_MAX_DISCOVERY_PAGE2_PER_DAY` | 72 (UTC day) |
| `EARLY_SCOUT_V4_PREFILTER_ENABLED` | `false` |

Streams recorded before the scout existed are adopted by a bounded, idempotent
application bootstrap. Migration `0012` creates only the schema.

Operating the scout on a schedule, the cockpit and run history are covered in
[scout-cockpit.md](scout-cockpit.md).

## Five-minute discovery (issue #77)

Shadow run B ([early-discovery-shadow-b.md](early-discovery-shadow-b.md))
measured `robinhood/new_pools` for 24 hours: about 156 new pools an hour, one
page of 20 covering about 7 minutes, and at a five-minute poll one poll in five
sharing no pool with the previous one. The current profile (15 minutes, first
10 pools) sees about a quarter of new pools; five minutes with the whole page
sees essentially all of them. The code supports that profile; every part that
changes provider or model volume is off or bounded by default.

**The whole page.** `EARLY_SCOUT_MAX_DISCOVERY_POOLS=20` processes the whole
page (the bound already allowed 20; the path is covered by a test). Every valid
pool is recorded; as many become watches as
`EARLY_SCOUT_MAX_NEW_WATCHES_PER_RUN` allows, so set it to at least 20 (40
with page 2), or the rest are declined as before.

**Overlap and possible gaps.** A pool counts as already seen when this system
recorded an observation of it before the run started. The reference is the
recorded observations themselves: no cursor or pool list is kept beside them,
so a restart neither loses the reference nor re-creates a watch (watches stay
unique per market stream, `uq_discovery_watch_stream`). A chain poll whose page
1 is full and shares no pool with that reference is counted in
`discovery_no_overlap_polls`; if nothing closes it, also in
`discovery_possible_gaps`, with its reason in the summary's
`discovery_gap_reasons`. A first run ever has no reference and records no gap;
a short page is the end of the list and is never a gap. These counters cost no
request and are kept in `scout_runs` (migration 0025) whether page 2 is on or
not.

**Page 2, only on proven saturation.** With
`EARLY_SCOUT_DISCOVERY_PAGE2_ENABLED=true`, such a poll reads page 2 once. It is
refused, and the gap recorded with its reason, when:

| Reason | Condition |
|---|---|
| `DISCOVERY_PAGE2_DISABLED` | the setting is off (default) |
| `DISCOVERY_PAGE_TRUNCATED` | page 1 was not processed whole (`EARLY_SCOUT_MAX_DISCOVERY_POOLS` < 20): page 2 would skip the rest of page 1 |
| `DISCOVERY_PAGE2_DAILY_BUDGET` | the UTC day's page-2 reads, summed over recorded scout runs, reached `EARLY_SCOUT_MAX_DISCOVERY_PAGE2_PER_DAY` |
| `DISCOVERY_PAGE2_RUN_BUDGET` | the run's GeckoTerminal transport (`GECKOTERMINAL_MAX_REQUESTS`) could not also cover page 1 of every later chain and one refresh per chain |
| `DISCOVERY_PAGE2_PROVIDER_*` | page 2 failed (for example `..._PROVIDER_RATE_LIMITED`); page 1 is kept, retries and `Retry-After` handling are the transport's unchanged ones |
| `DISCOVERY_PAGE2_NO_OVERLAP` | page 2 was read and still reached no recorded pool |

Page 2 is one request on the same transport and budget as page 1, never a
loop; the adapter refuses any page but 1 and 2. Its pools are discovery data
like page 1's; pools repeated across the page boundary are counted once.

**V4 prefilter.** Only a 32-byte V4 pool id can reach `LP_CONTROL=PASS`; a
20-byte pool address (V2/V3-style) is `UNSUPPORTED` (issue #92), so it can
never be bought. With `EARLY_SCOUT_V4_PREFILTER_ENABLED=true` a 20-byte pool is
still recorded and watched, with its history schedule and outcome labelling,
but:

- it is not due for a scout ORBIT review and not counted as review debt
  (`WatchRepository(orbit_v4_only=True)`);
- the early intake (`EarlyWatchCandidates`) does not read it, so no early case
  is opened and ATLAS spends no holder or V4 census reads on it.

The PROMOTABLE path of the normal VECTOR intake and the JEV shadow are
unchanged. Off by default; off, nothing changes.

**Cadence.** The scout runs under launchd every 900 s. `ops/scout/install.sh`
takes `RH_AGENTS_SCOUT_INTERVAL_SECONDS` (whole seconds, 300 to 3600) to render
another interval; 300 is the five-minute cadence. It is an install-time choice:
an installed agent keeps its interval until reinstalled. At 300 s, keep ORBIT
reviews per run small enough to finish inside the interval (launchd never
overlaps runs, and the advisory lock turns a late overlap into
`ALREADY_RUNNING`).

**Provider budget.** Page 1 every five minutes is 288 `new_pools` requests a
day per chain; with page 2 on every non-overlapping poll about 345 (≈ 57 more),
capped at 72 page-2 reads a day by default. The public limit (30 per minute) is
not approached. Downstream cost is bounded separately: ORBIT per run and per
day, ATLAS census `max_requests`; the prefilter roughly halves what reaches
them.

**Runtime change for the recommended profile** (not applied by this code):
`EARLY_SCOUT_MAX_DISCOVERY_POOLS=20`, `EARLY_SCOUT_MAX_NEW_WATCHES_PER_RUN=40`,
`EARLY_SCOUT_DISCOVERY_PAGE2_ENABLED=true`, `EARLY_SCOUT_V4_PREFILTER_ENABLED=true`,
migration 0025 applied, and the agent reinstalled with
`RH_AGENTS_SCOUT_INTERVAL_SECONDS=300`. Size the ORBIT daily cap to the V4
share.

## Provider identity and coverage

GeckoTerminal names a chain's **native asset** as a token resource at the zero
address (`bsc_0x0000…0000`, declared as BNB with 18 decimals). This is the same
convention Uniswap V4 uses for native currency, and four.meme bonding curves
trade against native BNB. Normalization accepts the zero address as a token
only in that exact role:

- the token resource must be bound to that same address on the resolved
  network, as for every token;
- it must declare the chain's native decimals (`Chain.native_decimals`, 18 on
  both configured chains);
- any other zero address, a pool at the zero address, base = quote, a
  contradicting resource binding, another network or another provider is still
  `provider_identity`.

The canonical asset id is `chain:mainnet:0x0000…0000`, so every downstream
contract that reads a token address sees the native sentinel exactly as the
provider sent it. ATLAS already refuses a zero base token (`TOKEN_ADDRESS_ZERO`),
and SIGNAL treats a zero base token as having no address. No symbol or name is
used for identity at any point.

Each scout summary reports discovery coverage:

```
discovered = valid_markets + provider_identity_rejects + other_provider_rejects
identity acceptance rate = valid_markets / discovered
watch creation rate      = watches_created / valid_markets
```

A live BSC `new_pools` sample on 2026-09-26 contained 20 pools. 10 of them were
rejected, all at this one rule (8 four.meme curves and 2 Uniswap V4 pools, each
quoted in native BNB), and there were no other identity failures.
