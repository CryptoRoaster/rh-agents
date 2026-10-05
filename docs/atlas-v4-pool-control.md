# ATLAS V4 pool control (`V4_POOL_CONTROL`)

## Why

Raw holder facts say which addresses hold a token. In Uniswap V4 every pool's
reserves sit in one PoolManager contract, so supply that one party can withdraw
at will — its liquidity position — shows up as a single technical holder, or
not at all when a holder provider filters that contract out. A historical
Robinhood Chain rug used exactly this: a second, never-traded V4 pool with a
creator-owned `beforeSwap` hook held ~57.5 % of supply in a creator-owned,
single-sided position while the visible top ten was ~9 %. The position was
withdrawn and dumped.

`V4_POOL_CONTROL` is a separate deterministic fact. The raw holder record is
never modified; an economic distribution is derived beside it.

## Census (chain-verified, bounded)

* **Pools:** PoolManager `Initialize` events with the token as `currency0`
  *or* `currency1` — so a pool nobody traded and no aggregator lists is found
  like any other. Native currency is the zero address; any pair works
  (MEME/MEME included). Each pool id is recomputed from its PoolKey
  (`keccak256(abi.encode(key))`); a mismatch is `V4_POOL_KEY_MISMATCH`.
* **Start block:** the origin source's creation block, itself proven on-chain
  (no code at `block - 1`, code at `block`). Unknown or contradicted is
  `V4_CREATION_BLOCK_UNKNOWN`, never guessed and never genesis.
* **Deployment:** configured per chain (`V4_DEPLOYMENTS`), never trusted by
  name. Every snapshot checks code at the PoolManager and each PositionManager
  and the PositionManager's immutable `poolManager()` binding at the pinned
  block (`V4_DEPLOYMENT_UNVERIFIED` otherwise).
* **Hooks:** permissions from the address bits (exact protocol semantics);
  code presence; read-only `owner()`. A revert or malformed answer is
  `OWNER_UNKNOWN`, never "no owner". Creator match only against a
  receipt-verified creator.
* **Positions:** rebuilt from `ModifyLiquidity` events per pool, then checked
  against the PoolManager's own storage (`extsload` of the position slot); any
  disagreement refuses the census. A verified PositionManager position records
  its current `ownerOf(tokenId)` exactly, with `getPoolAndPositionInfo` binding
  the token id to this pool and range; what that owner means for control is a
  separate fact (see [position control](atlas-v4-position-control.md)). A direct position belongs to its owner key
  only when that key is an externally owned account (or an EIP-7702 delegated
  one); a contract key is `OWNER_UNKNOWN`.
* **Amounts:** canonical `TickMath`/`SqrtPriceMath` in integers at the pool's
  current `sqrtPriceX96` (Slot0 via `extsload`), rounded down; in-range,
  below-range and above-range (single-sided) positions are exact.

Bounds (`CensusBounds`): block chunk 10 000, at most 240 requests, 16 pools,
4 000 position events, 64 active positions, 30 s wall clock; sequential reads
only. `eth_getLogs` is additionally capped at 100 000 blocks and a page of
5 000 logs is treated as possibly cut. Reaching any bound, and any RPC failure
or timeout, is an unavailable census with a reason — never a short list
reported as complete. A source answering for another chain is a hard refusal.

## Economic concentration

* The PoolManager's own holder row (if a provider reported one) leaves the
  distribution; each attributed position's amount is added to its controller
  (an account owner, or a verified release's controller), permanently locked
  supply goes to no holder, and unresolved custody makes the figure unknown; the
  PoolManager balance not traced to an owner is `UNATTRIBUTED_POOL_BALANCE`,
  ranked as one pseudo-holder. Nothing is counted twice.
* Owners outside the retained raw rows are ranked with the smallest retained
  balance added, unless the holder set was complete and retained whole.
* Where a quantity is only bounded the figure is labelled `UPPER_BOUND`: it may
  overstate concentration, never understate it.
* An unattributed remainder above 1 % of supply (`MAX_UNATTRIBUTED_POOL_FRACTION`
  — a completeness bound for fees and rounding, not a concentration limit)
  makes the economic figure unknown. This is also the safety net for anything
  the census missed, such as a pool initialized before the token existed.

Recorded: `economic_top1/5/10_share`, `pool_held_`, `attributable_pool_`,
`creator_controlled_pool_` and `unattributed_pool_supply_fraction`, pool,
hook and position counts, every pool id, the listed positions, and the raw
top-ten beside the economic one.

## Fail closed

Pool control is *required* for a market addressed by a bytes32 pool id, and
for any token whose census found V4 pools. Required and not established:

* ATLAS records `V4_POOL_CENSUS_UNAVAILABLE`, `V4_POSITION_FACTS_INCOMPLETE` or
  `V4_POOL_BALANCE_UNATTRIBUTED`; the holder domain is unestablished.
* Risk data records `ECONOMIC_CONCENTRATION_UNKNOWN` for holder concentration —
  legacy evidence for a V4 case included.
* SENTINEL's holder concentration for a purchase is the economic figure, or
  unknown; it never falls back to the raw one.

A census that breaks off after proving pools keeps them required
(`pools_found`), and a V4 market whose own pool the census did not find is
incomplete (`V4_MARKET_POOL_NOT_FOUND`).

Other markets keep exactly the holder path they had. A sale is not judged on
concentration, so the read behind an exit (`ATLAS_EXIT_POLICY_V2`) does not wait
on pool control, runs no census, and a V4 position can always be sold.

SENTINEL's existing `max_top_ten_holder_fraction` (0.35) judges the economic
figure; no new limit exists. Note the consequence: liquidity held in one
position counts for that position's controller, exactly as a V2 pair counts as
a holder in the raw view -- unless verified code proves nobody can take it.

## Evidence and compatibility

`OnchainIntelligence.pool_control` is a bounded summary (all pools, at most 16
listed positions, complete counts). It is omitted when absent, so evidence
written before it — and every non-V4 snapshot without a census — replays byte
for byte, and the snapshot digest changes only where pool control was
collected. No migration.

## Activation

`ATLAS_V4_POOL_CONTROL_ENABLED=false` by default: no census read is made, and a
V4 market's holder concentration stays unestablished. Enabling it composes the
census (and creation-receipt verification) over each chain's existing
read-only RPC client.

## Known limits

* **History budget.** With the default bounds (10 000-block chunks, 240
  requests) a token's history is covered for roughly a day of Robinhood Chain
  blocks; older tokens end as `V4_POOL_CENSUS_BOUNDS_EXCEEDED` — fail closed.
  Chunk size and budget must be tuned against the live RPC's `eth_getLogs`
  limits before activation.
* **Holder/chain skew.** Provider holder rows belong to their own snapshot,
  pool facts to the pinned block; a withdrawal between the two is bounded only
  by the existing source-skew policy.
* **Fees.** Uncollected swap fees in the token stay in the PoolManager; a very
  active pool can exceed the 1 % remainder bound and fail closed.
* **Flag on changes more than V4.** Enabling the census also verifies creation
  receipts and records pool control for every token, which changes the origin
  facts and the digest of non-V4 snapshots.
