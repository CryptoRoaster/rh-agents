# ATLAS V4 position control

[Pool control](atlas-v4-pool-control.md) puts supply held in Uniswap V4
positions back with the party behind each position. The census reads a
PositionManager position's ERC-721 owner with `ownerOf`. That answer says who
has custody of the NFT. It does not say who can take the principal. A contract
that holds the NFT may hand it to anyone, to one fixed party later, or to
nobody ever. So `NFT owner == contract` never means "this contract controls
the supply".

`POSITION_CONTROL` is a second fact, recorded per position beside the raw owner
(`src/agents/atlas/v4/control.py`).

| State | Meaning | Economic treatment |
|---|---|---|
| `DIRECT_CONTROL` | Owner is an account (no code, or an EIP-7702 delegation). | Supply counts for that account. |
| `PERMANENTLY_LOCKED` | Owner's code is a pinned official build with no path by which the principal ever leaves. | Recorded as locked liquidity. No holder is credited with it. |
| `TIMELOCKED` | Owner's code is a pinned official build that releases to a fixed controller at a fixed block, and that block is still ahead. | Unresolved: figure unknown, fail closed, floor recorded. |
| `RELEASABLE` | Same contract, with the block reached. | Supply counts for the controller. |
| `UNKNOWN_CONTRACT_CUSTODY` | Owner is a contract that no adapter verified. | Unresolved: figure unknown, fail closed, floor recorded. |

Each fact carries:

- `position_owner` (the raw `ownerOf`), `owner_kind`, `control_state`,
  `controller`, `unlock_block`, and `unlock_timestamp`. The timestamp is
  always absent: a future block has no proven time.
- `proof_kind`, `proof_contract`, `proof_version`, the owner's runtime-code
  keccak, `completeness` and, for unknown custody, a `refusal`.

## No heuristics

None of these establishes a lock:

- a name, symbol, explorer label, website or UI claim;
- "locker" in a name;
- a known address without a code check;
- how long a contract has held the NFT;
- the absence of transfers.

A state other than `DIRECT_CONTROL` or `UNKNOWN_CONTRACT_CUSTODY` rests on
verified code. The fact model refuses any other combination.

## Adapter boundary

`PositionCustodyAdapter` is the protocol boundary (`control.py`). Each adapter
returns one of three answers for an owner's exact code:

- `None` when it does not recognise the code;
- a verified verdict;
- `UNKNOWN_CONTRACT_CUSTODY` with a refusal, when the code is recognised but a
  binding fails.

The resolver (`custody/resolver.py`) gives every distinct owner to each adapter.
Code reads are charged to the census's request budget and wall-clock bound.
When no adapter recognises a contract, the resolver checks only whether it is
a proxy (EIP-1167 code, EIP-1967 implementation or beacon slot) and records
`PROXY_INDIRECTION` or `CODE_NOT_RECOGNISED`. Economic accounting
(`economic.py`), ATLAS policy, risk data and SENTINEL consume only the
normalised facts. None of them knows any protocol. More verified lockers can
be added as further adapters.

Code identity uses a template (`custody/template.py`):

- the runtime code of a pinned build with every `immutable` range zeroed,
  pinned by length and keccak256;
- the byte offsets of each immutable.

Deployed code matches only if it has exactly that length, one value at every
occurrence of each immutable, and the pinned hash once those ranges are
zeroed. ABI look-alikes, one-byte edits, appended code and proxies do not
match.

## First adapter: official Uniswap Liquidity Launcher

Source: `https://github.com/Uniswap/liquidity-launcher`.

Templates (`custody/uniswap_launcher.py`):

- **Build:** commit `7ea523c9d75a51cb2f497be5e49bacdaeb80a342` ("release: v3.3.0",
  the deploy commit the v3.3.0 README names for the FeeSplitters).
- **Settings, from the repository's `foundry.toml`:** solc
  `0.8.26+commit.8a97fa7a`, optimizer 200 runs, EVM `cancun`,
  `bytecode_hash = "none"`.
- **`FeeSplitter`:** 7260 bytes, masked keccak
  `0x7a79bab4…ecb77e`, immutables `positionManager` and `poolManager`. With
  the Robinhood managers filled in, the runtime keccak is
  `0x8238e510…325f42`.
- **`TimelockedPositionRecipient`:** 707 bytes, masked keccak
  `0x7cbaa225…6d2f77`, immutables `positionManager`, `operator`,
  `timelockBlockNumber` and BlockNumberish's `_USE_ARB_SYS`.

### FeeSplitter: PERMANENTLY_LOCKED

The verdict requires all three of the following:

1. The code is exactly the template.
2. The owner is a FeeSplitter that the launcher deployed on this chain. The
   versioned registry is keyed by chain and pinned to its chain id; a chain
   name answering for another id is a hard refusal.
3. The code's own `positionManager` and `poolManager` immutables equal both
   the PositionManager and PoolManager that the census verified and the
   registry entry.

Official code at an unlisted address is `DEPLOYMENT_MISMATCH`, so it stays
unknown. `ownerOf == FeeSplitter` and the position's pool and token-id binding
come from the census itself.

**Source audit of `src/periphery/FeeSplitter.sol` at the pinned commit:**

- **External surface:** `getSplits`, `collectFees`, `increaseLiquidity`,
  `onERC721Received` (a view that only checks the caller), `receive`, plus
  constant and immutable getters.
- **No way out for the NFT:** no NFT transfer, `approve`,
  `setApprovalForAll` or burn. There is no `isValidSignature` and no fallback,
  so the PositionManager's ERC-721 permit cannot approve anyone either.
- **`collectFees`:** runs exactly `DECREASE_LIQUIDITY(tokenId, 0, 0, 0)` plus
  `TAKE_PAIR` to the splitter itself. A zero-liquidity decrease realises fees
  and nothing else.
- **`increaseLiquidity`:** runs `UNWRAP, SETTLE, SETTLE, INCREASE_LIQUIDITY,
  TAKE_PAIR`. Liquidity only grows, and `_requireNoPendingFees` rejects calls
  while fees are pending. The final `TAKE_PAIR` returns only what the caller
  overpaid.
- **Nothing that could change the code or its powers:** no owner, admin,
  upgrade path, proxy, `delegatecall`, `selfdestruct` or arbitrary call. The
  only storage is the fee split, written once in the constructor.

Fee splits route fees to the beneficiary vault and the compounder. Neither
gains any power over the principal.

**Registry (Robinhood, chain id 4663):**

| Address | Version | Deploy commit | Proof |
|---|---|---|---|
| `0x9411fa7f956f64aa7981aa27cb3bc6ec0415449c` | v3.3.0 | `7ea523c` | README + offline CREATE2 reproduction + live template match |
| `0x882ae5e2095435a62fd1bbdefcb637f5ceafc0ee` | v3.3.0 | `7ea523c` | README + offline CREATE2 reproduction + live template match |
| `0xeff166aaf189323c58dc27ed1206eb2c37faacdf` | v3.2.0 | `dd8769cd` | README + live template match |
| `0x222d6d4f1ce59b0d48d5505114ec8addc90a4359` | v3.2.0 | `dd8769cd` | README + live template match |

**Offline CREATE2 reproduction (v3.3.0):** each address is reproduced from:

- the pinned build's creation code;
- the README's fee splits (40 % native to UERC20BeneficiaryVault
  `0x26d2…2553` and 60 % native plus 100 % token to CompoundingClaimRecipient
  `0xf585…c58d`; or 100 %/100 % to the compounder);
- PositionManager `0x58da…4fa7` as the constructor argument;
- salt `0` and the CREATE2 deployer `0x4e59…956c`.

Both results equal the README addresses exactly. That proves the deployed
creation code is the pinned build without any RPC call.

**v3.2.0:** commit `dd8769cd` has the identical FeeSplitter source, and its
build produces identical creation code. Its constructor arguments or salt
could not be reproduced offline. It is versioned separately and accepted only
through the same live template and binding checks.

### TimelockedPositionRecipient: TIMELOCKED or RELEASABLE

The contract has an immutable `positionManager`, `operator` and
`timelockBlockNumber`.

- `approveOperator()` reverts while `blockNumberish < timelockBlockNumber`.
  From that block on, anyone may call it, and it runs
  `setApprovalForAll(operator, true)` on the PositionManager.
- It has no transfer, no `onERC721Received` and no `isValidSignature`.

The launcher keeps no registry of these; each is deployed for one migration.
What is proven is the code (the exact template) and its immutables:

| Check | If it fails |
|---|---|
| `positionManager` is the verified PositionManager | `POSITION_MANAGER_MISMATCH` |
| `operator` is a non-zero address | `IMMUTABLE_INVALID` |
| `_USE_ARB_SYS == 1` and the chain registry declares the ArbSys L2 block clock | `BLOCK_CLOCK_UNSUPPORTED` |

On the last check:

- BlockNumberish v1.1.0 (`38fe20bc`) reads `arbBlockNumber()` when an ArbSys
  precompile answered at construction. On an Arbitrum Orbit chain that is the
  L2 block number the snapshot is pinned to.
- Without ArbSys it reads `block.number`, which is an L1 figure there.

Outcome at the pinned block:

- **Before `timelockBlockNumber`:** `TIMELOCKED`.
- **At or after it:** `RELEASABLE` to `operator`, which is attributed exactly
  like any controller. An operator that is the creator adds to the creator's
  controlled supply.

## Policy

- **The 35 % limit is unchanged, and there is no new holder limit.** SENTINEL
  still judges the economic top ten. The only change is which supply counts as
  economically controllable.
- **`PERMANENTLY_LOCKED`:** counts for no holder, and the denominator stays the
  on-chain `totalSupply`. Raw holder facts are untouched. For an official
  InstantLaunch token, the raw view is PoolManager-dominated, while the
  economic view holds only the buyers. Such a token is no longer refused for
  concentration alone. Every other rule still applies.
- **`RELEASABLE`:** counts for its controller.
- **`TIMELOCKED` and `UNKNOWN_CONTRACT_CUSTODY`:**
  - The pool-control status is `V4_POSITION_CONTROL_UNRESOLVED`, and ATLAS
    records the gap `V4_POSITION_CONTROL_UNRESOLVED`.
  - Risk data reports `ECONOMIC_CONCENTRATION_UNKNOWN`.
  - SENTINEL's holder check for a BUY is forced to UNKNOWN.
  - Only a floor is kept: the top ten without the unresolved supply, with
    nobody credited for more than was seen. A floor above the limit is an
    established violation: ATLAS raises a blocker under a limited policy, and
    SENTINEL raises `HOLDER_CONCENTRATION_LIMIT`. A floor below the limit
    proves nothing.
  - No minimum lock duration exists. A timelock is never a safe pass until a
    separate holding-horizon policy is decided.
- **A proven empty distribution is exactly zero.** At T+0 an official launch
  can have one holder, the PoolManager, and one permanently locked position.
  After the PoolManager row leaves the ranking, nothing is left. That empty
  ranking is reported as top 1, 5 and 10 = `0` with basis `EXACT`, against the
  unchanged total-supply denominator, and only when **all** of the following
  hold:
  - the holder set is `COMPLETE` and retained whole (below the retention cap);
  - the provider's holder count, if given, equals the rows;
  - no provider exclusion is unreconciled;
  - the rows account for the entire on-chain supply and are the PoolManager
    alone;
  - the PoolManager holds the whole supply;
  - there is no unattributed remainder and no unknown owner;
  - every PoolManager unit is in `PERMANENTLY_LOCKED` positions, with nothing
    direct, releasable, timelocked or of unknown custody.

  Anything less behaves as before:
  - a `TOP_N_ONLY` prefix, a count above the rows, an exclusion, or supply
    outside the rows: `V4_HOLDER_BASIS_UNAVAILABLE`;
  - a small remainder: ranked as `UNATTRIBUTED_POOL_BALANCE` with basis
    `UPPER_BOUND`;
  - a remainder over 1 %: unknown;
  - unresolved custody: `V4_POSITION_CONTROL_UNRESOLVED`.

  Zero means "no economically controllable principal was found". It does not
  mean "safe".
- **Exits are never bound:** a sale keeps its raw figure.
- **ATLAS limited policy:** a token read through pool control is no longer
  also judged on its raw top ten, which counts the PoolManager as one holder.
  The production policy sets no ATLAS limit.

The REVENUE mechanism is unchanged. Its live reference point is block
77 205 381, the last block before the creator's position was decreased to zero
(77 205 382). The dump followed at 77 205 425, so block 77 205 424 already shows
the supply in a wallet rather than in a position. The creator-owned position is
`DIRECT_CONTROL` (~57.4 %). With the launch NFT in an unverified locker, the
figure is unknown and its floor exceeds 35 %. With it in the official
FeeSplitter, the established figure exceeds 35 %. A creator's own position
moved into any unverified contract stays unknown and is never locked.

## Evidence and compatibility

**Durable record.** Each listed `PoolControlPosition` carries a `control`
record. `PoolControlSummary` carries:

- the locked, timelocked, releasable and unknown-custody amounts, each with its
  fraction of supply;
- per-state counts;
- the floor.

**Compatibility with older rows.** Every new key is omitted while empty, so
summaries written before position control replay byte for byte.

**Digest.** The snapshot digest covers each position's control facts, the
bucket amounts and the floor.

**Migration and RPC.** No migration and no new RPC method. The census uses
the existing `eth_getCode` and `eth_getStorageAt` reads, within its existing
budget.

## Rebuilding the templates

```sh
git clone https://github.com/Uniswap/liquidity-launcher && cd liquidity-launcher
git checkout 7ea523c9d75a51cb2f497be5e49bacdaeb80a342
git submodule update --init --recursive
forge build src/periphery/FeeSplitter.sol src/periphery/TimelockedPositionRecipient.sol
```

`deployedBytecode.object` and `deployedBytecode.immutableReferences` give the
template and its offsets. The unlinked code already carries zeros in every
immutable range. Its keccak must equal the pinned `masked_keccak`. The test
fixtures `backend/tests/atlas/v4/fixtures/*.runtime.hex` are exactly these
objects.

The CREATE2 check: `keccak256(0xff ‖ 0x4e59b44847b379578588920ca78fbf26c0b4956c ‖ 0x00…00 ‖ keccak256(creationCode ‖ abi.encode(positionManager, splits)))`.

## Not in scope

- Third-party Robinhood launchpads that claim "permanently locked liquidity".
  They are unknown until their deployment, bytecode binding, upgradeability,
  principal withdrawal, NFT transfer and admin paths are verified separately.
- Live verification against the chain. The adapters have been exercised only
  against fixtures. `ATLAS_V4_POOL_CONTROL_ENABLED` stays `false`.
