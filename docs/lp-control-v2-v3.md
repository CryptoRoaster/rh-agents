# V2/V3 LP control: architecture and live-evidence plan (issue #102)

Status: architecture and adapters only. **No V2/V3 BUY can pass.** The
production deployment registry and the locker adapters are empty, the code is
not wired into `lp_control_view`, and the risk engine accepts a PASS only on the
V4 pool-census basis (`LP_CONTROL_EVIDENCE_INCONSISTENT` otherwise). Every
20-byte pool stays `LP_CONTROL=UNSUPPORTED LP_CONTROL_SOURCE_ABSENT` (PR #95).

## Why

Since PR #95 every BUY needs `LP_CONTROL=PASS`. Only the V4 census can supply
it, and after issue #97 (PRs #100/#105) only when every position in the market
pool is `PERMANENTLY_LOCKED`. In the discovery shadow run (PR #101) 45 % of new
Robinhood pools are 20-byte (V2/V3-style), the largest venue `pons-v2`. This
change prepares a sound source for them without enabling one.

## Package `backend/src/agents/atlas/lpcontrol/`

| Module | Role |
|---|---|
| `models.py` | Typed evidence: `V2LpEvidence` (pair binding, LP `totalSupply`, every holder's balance and custody), `V3LpEvidence` (pool binding, positions, NPM and active-liquidity reconciliation), `CustodyFacts` per holder, `HolderState` -> `Withdrawal` (RESTRICTED / WITHDRAWABLE / UNRESOLVED). |
| `registry.py` | `LpDeploymentRegistry` of `V2Deployment` (factory, exact pair code hash, start block) and `V3Deployment` (factory, NonfungiblePositionManager, pool `CodeTemplate`, start block). `LP_DEPLOYMENTS` is **empty**; a guard test fails on any entry. |
| `reads.py` | `LpChainReads` read-port protocol (chain id, block time, code, call with full calldata, storage, logs), `BudgetedReads` (request budget, chunked log scans), exact ABI helpers. No implementation reaches RPC. |
| `custody.py` | `classify_holder`: burn address -> BURNED; the pair itself -> POOL_REDEEMABLE; no code / EIP-7702 -> DIRECT_CONTROL; contract -> verified `LpLockerAdapter` (pinned `CodeTemplate`, as the V4 custody adapters) or UNKNOWN_CONTRACT_CUSTODY with a refusal (proxy indirection detected). `DEFAULT_LOCKERS` is **empty**. |
| `v2_census.py` | `census_v2` against the port (below). |
| `v3_census.py` | `census_v3` against the port (below). |
| `decision.py` | Pure `decide_v2` / `decide_v3`. |

### Holder states and withdrawal classes

| HolderState | Established by | Withdrawal |
|---|---|---|
| BURNED | holder is `0x0` or `0x…dEaD` (includes V2 `MINIMUM_LIQUIDITY`) | RESTRICTED |
| PERMANENTLY_LOCKED | verified locker code + bindings, no release path | RESTRICTED |
| DIRECT_CONTROL | no code (or EIP-7702 delegation only) | WITHDRAWABLE |
| RELEASABLE | verified locker past its unlock block; controller named | WITHDRAWABLE |
| POOL_REDEEMABLE | LP tokens held by the pair: anyone can `burn()` them | WITHDRAWABLE |
| TIMELOCKED | verified locker before its unlock block | UNRESOLVED |
| UNKNOWN_CONTRACT_CUSTODY | unrecognised contract, proxy, or failed binding | UNRESOLVED |

A V3 position whose custody was not read is UNRESOLVED.

### V2 census (`census_v2`)

At one pinned block: chain id; `pair.token0/token1/factory()`,
`factory.getPair(token0, token1) == pair`, pair runtime-code keccak equal to the
registered template; the factory's `PairCreated` log for the pair (one request
with the discovery creation-block hint, else a chunked scan from the factory's
start block); every LP `Transfer` from creation, in chunks; candidates = every
address that ever received LP plus `0x0`; `totalSupply()` and every candidate's
`balanceOf`. Each nonzero candidate's balance must equal its Transfer
reconstruction (fee-on-transfer / rebasing LP fails here), and the read balances
must sum **exactly** to `totalSupply()`. Since balances are non-negative, that
equality proves nobody outside the set holds LP. The zero address is read, never
reconstructed: `_mint(0x0, MINIMUM_LIQUIDITY)`, a user `transfer` to `0x0` and
the pair's `_burn` all emit `Transfer(…, 0x0)` but only the first two credit it.

### V3 census (`census_v3`)

At one pinned block: chain id; `pool.token0/token1/fee/factory()`,
`factory.getPool(...) == pool`, pool code matching the registered template with
the factory/token0/token1/fee immutables bound to those reads,
`npm.factory() == factory`; the factory's `PoolCreated` log; every pool `Mint`
from creation (position keys); `pool.positions(key).liquidity` per key; the
NPM's `IncreaseLiquidity` logs only in the blocks where it minted into the pool;
`npm.positions(tokenId)` binds each token to this pool and range (a revert is a
burned, emptied token); per NPM key the tokens' liquidity must sum **exactly**
to the pool's; `slot0().tick` and `liquidity()`, with the in-range keys summing
**exactly** to it; `ownerOf` per NFT with liquidity, classified like a V2
holder; direct (non-NPM) positions classified by their own owner.

Every failure, revert (except the burned-token case), decode error, budget
exhaustion or limit breach is a named gap (`V2_*`/`V3_*`), never a truncated
answer.

### Decision (`decide_v2` / `decide_v3`)

Same rules as V4 after #97, in this order:

1. no registry entry for the market's chain, or for the evidence's factory
   (V3: and position manager) on that chain id -> **UNSUPPORTED**
   (`V2_DEPLOYMENT_UNREGISTERED`, `V2_FACTORY_UNREGISTERED`, …);
2. absent evidence, evidence of another chain, a census gap, another pool or
   token, an unverified factory binding, an incomplete scan, an unreconciled
   holder/position set, an empty pool, an unknown block time -> **UNKNOWN**;
3. any nonzero LP balance / position liquidity the verified creator can
   withdraw -> **BLOCKED** (`*_CREATOR_CAN_WITHDRAW`);
4. any unresolved custody -> **UNKNOWN** (`*_LP_CUSTODY_UNRESOLVED`);
5. any other withdrawable liquidity -> **UNKNOWN** (`*_LIQUIDITY_WITHDRAWABLE`);
6. everything RESTRICTED -> **PASS** (`V2_LP_TOKEN_CENSUS` / `V3_POSITION_CENSUS`).

Out-of-range V3 positions count like in-range ones: they are withdrawable all
the same.

## Tests (`backend/tests/lpcontrol/`)

- `test_v2v3_decision.py`: positive and negative decision cases on evidence
  built in `v2v3_fixtures.py` (FIXTURE ONLY, invented addresses).
- `test_v2v3_census.py`: both censuses end to end against `v2v3_chain.py`, an
  in-memory chain deriving every answer from declared protocol operations, with
  a FIXTURE locker adapter; dropped logs, fee-on-transfer, wrong factory, wrong
  pool immutables, chain mismatch, failed reads, budget and holder limits.
- `test_v2v3_registry.py`: production registry and lockers empty;
  `lp_control_view` still UNSUPPORTED on every 20-byte venue of the shadow run;
  the engine refuses a forged PASS on a V2/V3 basis.
- The existing RED regressions (`test_red_regression.py`, `test_end_to_end.py`)
  are unchanged and still pass.

## Live evidence needed (requires separate approval for RPC reads)

Per chain (Robinhood Chain, id 4663), per venue seen in the shadow run
(`uniswap-v2-robinhood`, `pons-v2`, `virtuals-robinhood`, `uniswap-v3-robinhood`,
`ramses-v3-robinhood`):

1. **Factory address**: from several recorded pools' `factory()`; confirm code
   present and that `getPair`/`getPool` returns those pools.
2. **V2 pair template**: runtime code of several pairs of the factory must be
   byte-identical; tie the keccak to a reproducible build of the claimed source
   (e.g. Uniswap v2-core, solc 0.5.16) or at least to the factory's
   `INIT_CODE_HASH` and CREATE2 address derivation. Forks with fee-on-transfer
   or non-standard pair code stay unsupported.
3. **V3 pool template and NPM**: masked runtime code of several pools equal,
   immutable offsets for factory/token0/token1/fee identified from a build;
   NonfungiblePositionManager address and `factory()` binding; confirm the
   NPM's `positions(uint256)` layout. Ramses-style forks (position keys with an
   index, gauge custody) need their own template and probably their own census.
4. **Factory start block** per deployment (bounds a scan without a hint).
5. **Lockers**: which locker contracts actually hold LP/NFTs on the chain
   (UNCX V2/V3 lockers, Team.Finance, PinkLock, launchpad vaults). Each needs a
   pinned source build, a `CodeTemplate`, and binding checks (pair/NPM it
   locks, owner/withdraw paths, unlock clock) before an adapter exists; until
   then such holders are UNKNOWN.

Reads per census (H = LP holders with a balance, U = unknown contracts, K =
position keys, B = NPM-mint blocks, T = NPM tokens, D = direct positions,
L = log chunks of 50 000 blocks over the pool's age; creation hint supplied):

- V2: `1 chain + 3 pair + 1 getPair + 1 code + 1 PairCreated + L + 1 totalSupply
  + (H+1) balanceOf + ≤H code + 2U storage + 1 timestamp ≈ 11 + L + 2H + 2U`.
  A fresh pair (L = 1, H ≤ 5): **~20–30 requests**.
- V3: `1 + 5 + 1 code + 1 npm.factory + 1 PoolCreated + L + K + B + T positions
  + 2 (slot0, liquidity) + T ownerOf + ≤T+D code + 2U + 1 ≈ 14 + L + K + B + 3T
  + D + 2U`. A fresh pool with 1–3 positions: **~25–35 requests**.

Without the creation hint the factory scan adds one request per 50 000 blocks
since the factory's deployment; the default budget is 600 requests.

## Decision points (owner)

1. Which factories / position managers to configure, after the on-chain
   verification above (needs RPC approval). Default: none.
2. Fee-on-transfer, rebasing or otherwise non-standard V2 forks: default
   UNSUPPORTED (pair template mismatch).
3. Whether `0x…dEaD` counts as burned (implemented: yes, as in the issue).
4. Whether a long TIMELOCKED lock may count as restricted (V4 #97 option 4);
   implemented: no, it is UNRESOLVED.
5. V3 completeness of out-of-range positions rests on the complete `Mint` scan
   (active liquidity reconciles only in-range keys); an extra reconciliation
   (tick `liquidityGross` or pool token balances) could be required before
   enabling.
6. Creator source: the receipt-verified creator from ATLAS origin evidence
   must be passed in; without it, creator-held liquidity is UNKNOWN, not BLOCKED.
7. Liquidity of the same token in other pools: V4 judges it for custody; here
   only the market pool is judged.
8. Enabling: wiring a V2/V3 census into ATLAS evidence, `lp_control_view`, the
   recorded `PoolControlSummary`, and extending `lp_control_refusal` to accept
   the V2/V3 bases are separate, explicitly approved changes.
