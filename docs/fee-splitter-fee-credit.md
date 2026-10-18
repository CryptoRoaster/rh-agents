# FeeSplitter fees and the V4 remainder credit (issue #103)

Why an official Robinhood launch still gets `LP_CONTROL=UNKNOWN`
(`V4_POOL_FEES_NOT_CREDITED`) once anyone has sold into its pool, what the
repository can prove offline, and exactly what is missing.

## Contracts in use (from this repository's registry and audit)

`backend/src/agents/atlas/v4/custody/uniswap_launcher.py`, documented in
`docs/atlas-v4-position-control.md`:

| FeeSplitter | Version | Deploy commit | How it is established |
|---|---|---|---|
| `0x9411fa7f956f64aa7981aa27cb3bc6ec0415449c` | v3.3.0 | `7ea523c` | README, offline CREATE2 reproduction, live template match at census time |
| `0x882ae5e2095435a62fd1bbdefcb637f5ceafc0ee` | v3.3.0 | `7ea523c` | same |
| `0xeff166aaf189323c58dc27ed1206eb2c37faacdf` | v3.2.0 | `dd8769cd` | README, live template match (constructor arguments not reproduced) |
| `0x222d6d4f1ce59b0d48d5505114ec8addc90a4359` | v3.2.0 | `dd8769cd` | same |

All bound to PositionManager `0x58da…4fa7` and PoolManager `0x8366…0951`.

## What the source audit establishes

- **Principal is permanently locked.** No function transfers, approves or
  burns a position NFT; `increaseLiquidity` only grows liquidity;
  `collectFees` runs a zero-liquidity decrease and `TAKE_PAIR` to the
  splitter itself. No owner, admin, upgrade, `delegatecall` or arbitrary call.
  The census classifies such a position `PERMANENTLY_LOCKED` (`RESTRICTED`).
- **Fees are routed, not locked.** Anyone can call `collectFees`; the fees land
  in the splitter and are paid out according to its splits, which are written
  once in the constructor.

## Fee splits (as far as recorded)

From the launcher README, used for the offline CREATE2 reproduction of the two
v3.3.0 addresses:

| Variant | Native (ETH) side | Token side |
|---|---|---|
| A | 40 % to `UERC20BeneficiaryVault` `0x26d2f7acb07707034406a0dc458351bb63c02553` (credits the creator), 60 % to `CompoundingClaimRecipient` `0xf585…c58d` | 100 % to `CompoundingClaimRecipient` `0xf585…c58d` |
| B | 100 % to the compounder | 100 % to the compounder |

Not recorded here: which of the two v3.3.0 addresses uses which variant, the
compounder's full address, and the v3.2.0 splits.

## Token side and ETH side, separately

- **ETH side.** Not token supply and not principal. It changes nobody's
  control of the pool's liquidity, so it never blocks LP control (offline
  regression: `tests/acceptance/test_early_paper_offline.py::test_eth_side_fees_of_the_locked_launch_do_not_block`).
- **Token side.** Token supply that leaves the PoolManager to whoever the
  splits name when `collectFees` runs. It belongs in the economic
  concentration with that recipient. Until the recipient is verified, the
  remainder credit cannot place it: the fee stays *uncredited*, the economic
  figure keeps one unattributed row, and LP control is `UNKNOWN`
  (`V4_POOL_FEES_NOT_CREDITED`). Any sell into the pool charges its fee in the
  token, so this applies to every traded official launch.

## Recipients and withdrawal rights (verified offline, 2026-10-18)

See `docs/fee-splitter-verification.md` for sources, CREATE2 reproduction and
the live reads still needed.

| Recipient | Receives | Who can take it | Status |
|---|---|---|---|
| `UERC20BeneficiaryVault` `0x26d2F7AcB07707034406a0dC458351Bb63C02553` | 40 % of native fees (variant A, splitter `0x9411…449c`) | the holder of the beneficiary NFT for the position | VERIFIED offline; native only, irrelevant to token concentration |
| `CompoundingClaimRecipient` `0xf585b5D728A8fdE743027307BF5F3556E3B9C58D` | 100 % of token fees (both variants), 60–100 % of native fees | **anyone**: `claim()` is permissionless and pays the caller, who must only raise the position's liquidity by `minLiquidityIncrease` (1e20) | VERIFIED from source and CREATE2; on-chain code match UNKNOWN until a live read |

Token-side fees of a FeeSplitter position are therefore claimable by an
arbitrary party and become nobody's locked principal. They cannot be credited
to a holder, and `V4_POOL_FEES_NOT_CREDITED` stays the correct outcome. The
v3.2.0 splitters (`0xeff1…`, `0x222d…`) remain PARTIALLY_VERIFIED.

## Remaining evidence

A pinned-block live read (13 reads): code templates and `getSplits()` of the
four registered splitters, and the code of the recipients
(`docs/fee-splitter-verification.md`). It can confirm the facts above; it
cannot make token fees creditable.
