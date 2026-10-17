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

Two tables (migration `0025`):

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
   hash of its last block — only if the coverage did not move meanwhile.
5. Every read counts against `max_requests`; running out keeps what was
   confirmed (`PARTIAL`), and the next run resumes there. A failed read is never
   "no pools".

The census (`V4PoolCensus.index`) asks it only for pools of one token
initialized *before* the token's creation block, and only when the coverage
reaches that block. Each such pool is re-checked exactly like a live Initialize
(key hash, token, block) and then read live at the census block like any other.
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
- Migration `0025` must be applied before either is used; the runtime
  preflight refuses a database behind the code's head.

## Live evidence still required (not executed; needs budget approval)

| Step | Reads | Estimate |
|---|---|---|
| Locate and verify the deployment block | `eth_getCode` ~log2(head) + 2 | ~30 |
| Backfill from deployment to head | 1 `eth_getLogs` + 1 header per 100 000-block window | Robinhood ≈ 10 blocks/s, so ≈ 2 × (chain age in blocks / 100 000); at ~77 M blocks ≈ 1 550 |
| Window narrowing on provider caps | extra `eth_getLogs` per refused window | budget ×1.2 |
| Incremental run every 5 minutes | ~3 000 blocks: chain id, head, tip hash, 1 log read, 1 header | ~5 per run, ~1 440 per day |

The PoolManager emits roughly 2 000 Initialize events per day on Robinhood
(shadow run B: ≈ 55 % of 3 741 new pools a day are V4-style), far below the
5 000-log cap per window. Run the backfill in several bounded calls (for
example `--max-requests 400` four times) and compare the result with a second,
independent RPC endpoint before enabling the census setting.
