# ATLAS: PoolManager-wide V4 pool index (issue #94)

## Why

v4-core initializes a pool for any currency address, whether or not code exists
there yet, and a token's address is known before it is deployed (CREATE nonce
or CREATE2). A census that searches for a token's pools from the token's
creation block can therefore miss a pool initialized earlier. Liquidity added
to such a pool later sits in the PoolManager as surplus that no found position
explains (independent review finding B1 on PR #99). Since PR #99 any surplus
over a token-creation discovery is `V4_POOL_SURPLUS_UNEXPLAINED`.

## What it is

Two tables (migration `0026`, after `0025` scout discovery gaps):

| Table | Content |
|---|---|
| `v4_pool_index_coverage` | per (chain id, PoolManager): verified deployment block, last confirmed block, that block's hash |
| `v4_pool_index_pools` | every Initialize in the coverage, any token: pool id, key, block |

`V4PoolIndexer.advance()` (`backend/src/agents/atlas/v4/pool_index.py`):

1. Verifies the RPC chain id against the configured deployment.
2. First run only: locates the PoolManager's deployment block by its own code
   presence (binary search, code at the block, none before) and starts there.
3. Re-reads the hash of the last confirmed block; if the chain no longer
   agrees, rewinds 1 000 blocks and deletes what lay beyond (reorg safety).
4. Reads `Initialize` logs of the PoolManager (no token filter) window by
   window up to `head − confirmations`, checks that every pool id hashes from
   its key and occurs once, and stores each window atomically together with the
   hash of its last block — only if its parent is still the confirmed block,
   that hash was the same before and after the logs were read, and the
   coverage did not move meanwhile.
5. Every read counts against `max_requests`; running out keeps what was
   confirmed (`PARTIAL`), and the next run resumes there. A failed read is never
   "no pools".

The census (`V4PoolCensus.index`) asks it only for pools of one token
initialized *before* the token's creation block, and only when the coverage
reaches that block. Each such pool is re-checked exactly like a live Initialize
(key hash, token, block) and then read live at the census block like any other.
The census first checks that the chain still has the index's last confirmed
block (one header read); a stale index is not used. The index answers only if
its coverage is the same before and after the pool read.
The census records `pool_discovery_basis` (`TOKEN_CREATION` or
`MANAGER_HISTORY`) and `pool_discovery_from_block`.

No stale approval can come from the index: it only lists which pools exist
below a confirmed block. Positions, balances, fees, custody and freshness are
all read at the census block, which is still bound to the ATLAS block and dated
by its block time (PR #95).

## Effect on LP control

| Surplus over … | Result |
|---|---|
| token-creation discovery (index off or not covering) | `V4_POOL_SURPLUS_UNEXPLAINED` |
| PoolManager-history discovery | owed to nobody, but still `V4_POOL_SURPLUS_NOT_CREDITED` until the economic figure stops ranking it (#103) |

## Operation (nothing is scheduled)

- Setting `ATLAS_V4_POOL_INDEX_ENABLED` (default `false`) makes the ATLAS census
  read the index.
- `python -m src.agents.atlas.v4.index_job --chain robinhood --max-requests N`
  advances it once. Exit `0` complete, `3` partial, `1` failed.
- Migration `0026` must be applied before either is used; the runtime
  preflight refuses a database behind the code's head.

## Live evidence still required (not executed; needs budget approval)

| Step | Reads | Estimate |
|---|---|---|
| Locate and verify the deployment block | `eth_getCode` ~log2(head) + 2 | ~30 |
| Backfill from deployment to head | 1 `eth_getLogs` + 3 headers (parent, and the last block before and after) per 100 000-block window | Robinhood ≈ 10 blocks/s, so ≈ 4 × (chain age in blocks / 100 000); at ~77 M blocks ≈ 3 080 |
| Window narrowing on provider caps | extra `eth_getLogs` per refused window | budget ×1.2 |
| Incremental run every 5 minutes | ~3 000 blocks: chain id, head, tip hash, 1 log read, 3 headers | ~7 per run, ~2 020 per day |

The PoolManager emits roughly 2 000 Initialize events per day on Robinhood
(shadow run B: ≈ 55 % of 3 741 new pools a day are V4-style), far below the
5 000-log cap per window. Run the backfill in several bounded calls (for
example `--max-requests 800` four times) and compare the result with a second,
independent RPC endpoint before enabling the census setting.

## Backfill readiness (dry run, no RPC)

`tests/atlas/v4/test_backfill_estimate.py` runs the production indexer over a
fixture PoolManager history of Robinhood size (deployment at block 1 000 000,
head 77 205 000, 12 confirmations) in bounded calls of 800 reads, each resuming
where the last stopped, and pins the read counts:

| Provider window | Windows | Calls of 800 | Reads |
|---|---|---|---|
| 100 000 blocks (client cap) | 763 | 4 | 3 095 |
| 25 000 blocks (narrower provider) | 3 049 | 16 | 12 351 |

Per window: the last block's hash before and after the logs, the logs, and the
parent block's hash after them (4 reads). Once: the deployment search
(⌈log₂ head⌉ + 2 ≈ 29). Per call: chain id, head and the tip check. A provider
cap is learned on the first window of every call (two refused probes) and kept
for the rest of the call. A call that runs out inside a window loses at most
that window's reads.

**Scaling.** Reads ≈ 4 × (head − deployment) / window + 29 + 3 per call. The
real deployment block and head are only known at run time; per million blocks
of history that is ≈ 40 reads at 100 000-block windows and ≈ 160 at 25 000.
At ten blocks a second the chain adds ≈ 0.86 M blocks a day (≈ 35 or ≈ 140
reads a day of backfill growth until it is run).

**Provider limits (from this repository's documentation and client).** The
RPC client refuses spans above 100 000 blocks and treats an answer of 5 000
logs or more as possibly cut (`RPC_CONTRACT`), which narrows the window; the
PoolManager emits about 2 000 Initialize events a day on Robinhood, far below
5 000 per window at either width. Rate limits, credentials, chain identity and
connectivity are never retried by narrowing.

**Worst case.** A provider that refuses even small windows narrows down to one
block; every attempt is a budgeted read, so a call ends `PARTIAL` at its
`--max-requests` (capped at 2 000 by the CLI) and the next call resumes. The
budget, not the provider, bounds every call.

**Failure scenarios.**

| Event | Effect |
|---|---|
| budget exhausted | `PARTIAL`; everything confirmed so far is kept; next call resumes |
| rate limit, credentials, connectivity | `FAILED`; confirmed coverage kept; next call resumes |
| timeout or provider error on a window | the window is narrowed for the rest of the call; `FAILED` only if even one block fails |
| forged or malformed Initialize | `FAILED`; that window is not stored |
| window's last block changed during the read | `FAILED`; not stored |
| parent block changed during the read | `FAILED`; not stored |
| recorded tip no longer canonical | next call rewinds 1 000 blocks and reads again |
| concurrent second run | compare-and-swap refuses the slower writer (`IndexConflict`) |
| chain id mismatch | `FAILED` before any read of the index range |

The census uses the index only when its coverage reaches the token's creation
block, is unchanged across the pool read and the chain still has its last
confirmed block; otherwise discovery starts at the token (fail closed).

**Live run, once approved.** Apply migrations 0025 and 0026, then run
`python -m src.agents.atlas.v4.index_job --chain robinhood --max-requests 800`
repeatedly until it exits `0`; budget 4 000 reads at 100 000-block windows
(≈ 3 100 expected plus margin), 13 000 if the provider caps at 25 000. Cross-
check the pool count and a sample of pools against a second RPC endpoint before
setting `ATLAS_V4_POOL_INDEX_ENABLED=true`.

