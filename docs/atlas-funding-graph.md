# ATLAS creator funding graph (V1 + V2 prelaunch, shadow)

`CREATOR_FUNDING_GRAPH` records which distinct addresses the token's origin
creator paid native currency to, directly and successfully, and how many of
them appear among the token's observed holders. It has two separate parts:

- **V1** -- the launch window, from the creation block to the pinned ATLAS
  block (`scope = DIRECT_NATIVE_FROM_ORIGIN_CREATOR`);
- **V2 prelaunch** -- the same creator in bounded lookbacks *before* the
  creation block (`prelaunch.scope = DIRECT_NATIVE_FROM_ORIGIN_CREATOR_PRELAUNCH`,
  `version = 2`).

V2 never changes a V1 field. Both come from one provider read.

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

## V2: prelaunch funding

V1 is blind to funding that happens before the token exists. The historical
REVENUE launch is the case that showed it: its creator paid 182 distinct
wallets directly, in 232 transfers, in the 20 minutes before creation -- 101 of
them exactly 0.01 native -- and paid nobody afterwards, so V1 measured zero.

A historical check confirmed the mechanism: 101 of the 126 wallets that later
bought the token had been funded this way, and 916 of the 948 buy transactions
came from them. That buyer overlap is **validation only**. It is not a
production input: V2 reads no buyer, no `eth_getTransactionByHash`, no sale.
The signal V2 measures exists at creation, before any buy does.

- **Windows:** `PT1H`, `PT6H`, `PT24H`. Each is
  `[creation_timestamp - lookback, creation block)`: no older than its cutoff,
  strictly before the creation block (which belongs to V1).
- **Edges:** exactly V1's definition (`funding_edges()`), only the window
  differs. No ERC-20, internal transfer, trace, second hop, label or cluster.
- **Creation time:** the creation block's header timestamp, read chain-side
  through the verified RPC client (`eth_getBlockByNumber`). Never estimated
  from block numbers. Unreadable: prelaunch is unavailable
  (`PRELAUNCH_CREATION_TIME_UNAVAILABLE`), the read is V1 only, and V1 is
  unchanged.
- **Transaction time:** Blockscout's `timestamp`, the transaction's block time
  (required in `pro-api-v12`, nullable while pending). Only the history uses
  it: a mined row without a timezone-aware value, or a timestamp increasing in
  the newest-first order, makes the history unusable (`history_failure`,
  prelaunch `UNAVAILABLE` / `INVALID_RESPONSE`). The read then ends exactly
  where a V1-only read ends, and **V1 is measured from the same pages as
  before** -- a V2-only data defect never voids V1. A prelaunch row later than
  the creation time, or the creation transaction (where the read contains it)
  at any other time than the chain's, likewise voids only the prelaunch
  measurement. A V1-only read does not read timestamps at all.
- **Coverage per window:** `COMPLETE` when the provider's list ended, or the
  validated read reached a row older than that window's cutoff; otherwise
  `LOWER_BOUND`. A short window can be exact while a longer one is not, and a
  lower-bound zero never means "none".
- **Per window:** edge count, distinct recipients, total value, first and last
  block and time, digest over every counted edge, distinct values, repeated
  edges, most edges to one recipient, the largest identical-value cluster
  (distinct recipients paid one exact value; ties go to the smallest value) with
  its value and share of all recipients, and the most distinct recipients in any
  10-minute span.
- **Sample:** at most 16 edges, for `PT24H` only.
- **Holders:** `PT24H` recipients among the observed holders, on the same basis
  as V1 (`holder_basis()`): for V4 the economic holders or `UNKNOWN`, never raw
  rows. An additional fact; prelaunch does not depend on it.
- **Provenance:** prelaunch carries the root, origin source and
  `origin_verification`. A measurement being available does not upgrade an
  `UNVERIFIED` root.

## Source (Robinhood, Blockscout PRO)

The contract is `GET /{chain_id}/api/v2/addresses/{hash}/transactions?filter=from`,
as specified in Blockscout's PRO OpenAPI document (`pro-api-v12`) and its
implementation:

- order is newest first (`block_number`, then `index`);
- keyset cursors in `next_page_params`, `null` at the end; the cursor echoes
  the request's documented `filter` parameter, accepted only as exactly `from`;
- 50 items per page;
- `Authorization: Bearer` header.

Blockscout documents no block-range filter, so the window is enforced here.
Coverage is proven only by the provider's order: the list ends, or a page
reaches a transaction older than the creation block.

With a creation time, the same read continues past the creation block until
the list ends, a row older than `creation_timestamp - 24h` is reached, or the
page budget runs out -- one read for V1 and every prelaunch window.

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
- `funding_graph.prelaunch` carries every window with its own coverage. It is
  omitted entirely where it was not measured, so a V1 record replays byte for
  byte.
- The snapshot digest covers the graph, and the prelaunch part where present.
- No migration.

## Configuration

`ATLAS_FUNDING_GRAPH_ENABLED` defaults to `false` and switches V1 and V2
together; there is no separate prelaunch flag. When set, the graph runs only
for Robinhood with Blockscout as the origin provider, and only on the entry
path, never in an exit read. BSC is deferred.
