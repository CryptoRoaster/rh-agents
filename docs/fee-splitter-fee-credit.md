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

## Recipients and withdrawal rights

| Recipient | Receives | Who can take it | Status |
|---|---|---|---|
| `UERC20BeneficiaryVault` `0x26d2…2553` | 40 % of ETH fees (variant A) | credits the creator | ETH only; irrelevant to token concentration |
| `CompoundingClaimRecipient` `0xf585…c58d` | 100 % of token fees (both variants), 60–100 % of ETH fees | **unknown**: if it compounds into the locked position, the token fees become locked principal (nobody's); if a party can claim them, they are that party's supply | **unverified** |

## Missing evidence (exact)

1. The compounder's full address (only `0xf585…c58d` is recorded).
2. The source of `CompoundingClaimRecipient` at the launcher's pinned commit,
   its runtime code template and immutables, and an audit of who can move what
   it receives.
3. For each registered FeeSplitter: its splits at the pinned block
   (`getSplits()` or the storage the constructor wrote), with the ABI or
   storage layout taken from the pinned FeeSplitter source.
4. The v3.2.0 splits (constructor arguments were not reproduced offline).

Items 1, 2 and the ABI/layout of 3 come from the public launcher repository
(no RPC); reading each splitter's splits and matching the compounder's code is
one RPC read per splitter plus one code read, at census time.

## What would change, once verified

A registry entry per FeeSplitter naming its verified token-fee recipient, and
a custody verdict for that recipient: `PERMANENTLY_LOCKED` (compounds into the
locked position) or a named controller. The remainder credit would then place
locked-position token fees with that recipient (or nobody), exactly, and only
for registered splitters; every other case stays uncredited. No such entry
exists today (offline regression: every registered splitter lacks a token-fee
recipient).
