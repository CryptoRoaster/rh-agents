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
| `EARLY_SCOUT_MAX_NEW_WATCHES_PER_RUN` | 1 |
| `EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_RUN` | 8 |
| `EARLY_SCOUT_MAX_HISTORY_CHECKS_PER_RUN` | 1 |
| `EARLY_SCOUT_MAX_REFRESH_MARKETS_PER_RUN` | 8 |
| `EARLY_SCOUT_MAX_BOOTSTRAP_STREAMS` | 100 |

Streams recorded before the scout existed are adopted by a bounded, idempotent
application bootstrap. Migration `0012` creates only the schema.

Operating the scout on a schedule, the cockpit and run history are covered in
[scout-cockpit.md](scout-cockpit.md).

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
