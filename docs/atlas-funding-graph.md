# ATLAS creator funding graph (V1, shadow)

`CREATOR_FUNDING_GRAPH` records which distinct addresses the token's origin
creator paid native currency to, directly and successfully, between the
token's creation block and the pinned ATLAS block. It also records how many of
those addresses appear among the token's observed economic holders.

It is a **measurement only**:
- no policy reads it, no ATLAS verdict, gap or blocker changes;
- risk data and SENTINEL are untouched;
- nobody is called a buyer, bot, insider or sybil.

A policy and any thresholds will be decided separately, after real data has
been measured.

## Scope

`scope = DIRECT_NATIVE_FROM_ORIGIN_CREATOR`, and nothing more:

- **Root:** `OriginFacts.creator_address` from the configured origin source.
  The record also carries the origin source, its verification state, the
  factory address and the creation block. The root is never extended through
  labels, ENS, explorer names or "probably the dev" heuristics. Without an
  available origin there is no graph (`FUNDING_ORIGIN_UNAVAILABLE`).
- **Window:** `creation_block <= block <= snapshot_block`. Later activity can
  never change an entry snapshot.
- **Edge:** a transaction counts only when all of these hold:
  - it succeeded;
  - `from` is exactly the root;
  - `to` is a concrete address other than the root;
  - its native value is greater than 0;
  - it lies inside the window.

  Failed transactions, zero-value calls, contract creations, self transfers,
  ERC-20 transfers, internal calls and second hops never count. A hash
  repeated identically counts once. A hash repeated with different contents
  refuses the read.
- **Values** are exact integers in wei, never converted to USD.

## Source (Robinhood, Blockscout PRO)

The contract is `GET /{chain_id}/api/v2/addresses/{hash}/transactions?filter=from`,
as specified in Blockscout's PRO OpenAPI document (`pro-api-v12`) and its
implementation:

- order is newest first (`block_number`, then `index`);
- keyset cursors in `next_page_params`, `null` at the end;
- 50 items per page;
- `Authorization: Bearer` header.

Blockscout documents no block-range filter, so the window is enforced here.
Coverage is proven only by the provider's order: the list ends, or a page
reaches a transaction older than the creation block.

**Bounds:** 10 pages, at most 500 normalized transactions. A read cut by the
page budget is `coverage = LOWER_BOUND`: every count is what was seen, and the
truth is that or more. It is never a complete zero.

Rows are refused when any of these apply:
- the sender is not the root;
- an address, hash, block, value, status or position is malformed;
- the order runs against the documented order;
- a cursor repeats or carries unknown keys;
- an empty page still promises more.

Pending rows are skipped.

## Holder overlap

- **V4 tokens:** measured against the reconciled economic holders of pool
  control. The PoolManager, locked principal and controller attribution are
  already settled there. If those holders are unknown, the overlap is
  `UNKNOWN`; raw rows are never a fallback.
- **Other markets:** measured against the normalized raw holders.
- **Fractions:** holder fractions are within the observed, bounded basis. The
  supply fraction is against the on-chain total supply.

## Evidence

- `OnchainIntelligence.funding_graph` carries every count, a sample of at most
  16 edges, and a sha256 digest over all counted edges. It is omitted entirely
  where nothing was collected, so earlier rows replay byte for byte.
- The snapshot digest covers the graph.
- No migration.

## Configuration

`ATLAS_FUNDING_GRAPH_ENABLED` defaults to `false`. When set, the graph runs only
for Robinhood with Blockscout as the origin provider, and only on the entry
path, never in an exit read. BSC is deferred.
