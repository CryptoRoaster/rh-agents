# FeeSplitter verification for V4 LP control (issue #103)

Offline verification from public, checkable sources only: the Uniswap
liquidity-launcher repository, built locally (forge 1.8.1, solc
0.8.26+commit.8a97fa7a, EVM cancun, 200 runs, `bytecode_hash none`) and its
addresses reproduced through CREATE2. No RPC call, no API key. Sources:

- R1: `https://github.com/Uniswap/liquidity-launcher/blob/7ea523c9d75a51cb2f497be5e49bacdaeb80a342/` (v3.3.0)
- R0: `https://github.com/Uniswap/liquidity-launcher/blob/266c18786c58822b72a7a9490f4e319294df0548/README.md` (repository head)

## Result

**Token-side fees of an official launch are permissionlessly claimable, not
locked.** The FeeSplitter forwards 100 % of token fees to the
`CompoundingClaimRecipient`; anyone may call its `claim()`, receives everything
attributed there, and only has to have increased the position's liquidity by
the fixed `minLiquidityIncrease` (1e20) by the end of the call. The excess is
the caller's. Token fees therefore never become locked principal and belong to
no fixed holder: `V4_POOL_FEES_NOT_CREDITED` (LP control UNKNOWN) is the
correct, fail-closed outcome, and must stay so.

## Findings

| # | Item | Status | Evidence |
|---|---|---|---|
| 1 | v3.3.0 `UERC20BeneficiaryVault` `0x26d2F7AcB07707034406a0dC458351Bb63C02553` and `CompoundingClaimRecipient` `0xf585b5D728A8fdE743027307BF5F3556E3B9C58D` on Robinhood (4663) | VERIFIED (offline) | R0 README; CREATE2 (deployer `0x4e59b44847b379578588920ca78fbf26c0b4956c`, salt 0) of the R1 build with vault arguments `(PositionManager, tokenJar 0x2aC03e14Cfe755426DaAEe0a4994184Ce81482F8, 0x…dEaD)` and compounder arguments `(PositionManager, 1e20)` from R1 `script/contracts/periphery/Deploy*.s.sol` and `script/contracts/Parameters.sol` |
| 1b | v3.2.0 vault `0xd35E9CA7…36D7`, compounder `0xf9526Dd3…E99a` | PARTIALLY_VERIFIED | named only in the R1 README; CREATE2 not reproduced |
| 2 | Compounder behaviour (R1 `src/periphery/CompoundingClaimRecipient.sol`, `BaseClaimRecipient.sol`, `BaseClaimRecipientWithCallback.sol`) | VERIFIED (source) | `claim()` permissionless, pays everything to `msg.sender`, calls `IClaimExecutor.onClaimed`, then requires liquidity ≥ before + `minLiquidityIncrease`; no owner, upgrade or `delegatecall`. Runtime 3 598 B, masked keccak `0x9e972eb22a07d6c76e5f2753f6eaf236e2f4ccbe9864d005b2d684564642a084` |
| 3 | FeeSplitter (R1 `src/periphery/FeeSplitter.sol`, `src/interfaces/IFeeSplitter.sol`) | VERIFIED (source) | `getSplits()` selector `0x520db906` → `(address recipient, uint16 nativeBps, uint16 tokenBps, bool useCallback)[]`; `_splits` length in slot 0, elements from `keccak(0)` + i, one packed slot each; `collectFees` permissionless, distributes all native and token by bps. Template `0x7a79bab4…ecb77e` (7 260 B) matches the census template |
| 4 | Split per registered splitter | `0x9411fa7f…449c` = A, `0x882ae5e2…f0ee` = B: VERIFIED (offline, CREATE2 reproduced); `0xeff166aa…cdf`, `0x222d6d4f…4359`: PARTIALLY_VERIFIED (README text only) | A = `[(vault, 4000 native, 0 token, callback), (compounder, 6000 native, 10000 token, callback)]`; B = `[(compounder, 10000, 10000, callback)]` (R1 `DeployFeeSplitter.s.sol`) |
| 5 | Creator share | VERIFIED (source), R1 `UERC20BeneficiaryVault.sol`, `BeneficiaryVault.sol` | variant A only: 40 % of **native** fees, 0 % of token fees, credited to the position's token id, withdrawable by the holder of the transferable beneficiary NFT; unregistered → native to tokenJar, token to `0x…dEaD` |
| 6 | Locking and admin | VERIFIED (source) for the v3.3.0 templates | FeeSplitter principal permanently locked, fees pass through; vault and compounder have no owner, proxy or `selfdestruct`, configuration in immutables only. Whether the deployed code matches the templates: UNKNOWN until a live read |

## Consequences for the decision

- `PERMANENTLY_LOCKED` stays correct for the launch position's **principal**.
- Token-side fees owed to it are **not creditable** to any holder and keep
  LP control `UNKNOWN`. ETH-side fees do not affect token supply and do not
  block.
- A possible later policy (decision point, not implemented): treat
  permissionlessly claimable token fees as worst-case — added in full on top
  of the largest holder for the concentration bound — instead of refusing.
  That would be an exact upper bound, not a tolerance, but it changes the
  meaning of `basis == EXACT` and needs its own review.

## Live evidence still required (after approval; all reads pinned to one block)

| Read | Count |
|---|---|
| block pin (`eth_getBlockByNumber`) | 1 |
| per registered splitter: `eth_getCode` (template and PositionManager/PoolManager immutables) and `eth_call getSplits()` (`0x520db906`) | 4 × 2 = 8 |
| v3.3.0 recipients: `eth_getCode` of compounder (template `0x9e972eb2…`, `minLiquidityIncrease == 1e20`) and vault (template `0x4352dda6…`) | 2 |
| v3.2.0 recipients named by `getSplits()`: `eth_getCode` each (template from the dd8769cd build) | ≤ 2 |
| **Total** | **13** (≤ 17 with storage reads instead of `getSplits()`) |
