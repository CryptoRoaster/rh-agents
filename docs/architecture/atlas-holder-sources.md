# ATLAS holder sources per chain

ATLAS requires HOLDERS for every case, and risk readiness then needs a holder
count, a proven top-ten figure and no provider-side exclusions. Plain EVM RPC
cannot enumerate holders, so each chain needs an indexer-backed source. One
provider per chain, selected by configuration, each disabled by default:

| Chain | Setting | Providers |
|---|---|---|
| Robinhood (4663) | `ATLAS_RH_HOLDER_PROVIDER` | `blockscout` (`BLOCKSCOUT_API_KEY`) |
| BSC (56) | `ATLAS_BSC_HOLDER_PROVIDER` | `nodereal` (`NODEREAL_API_KEY`), `moralis` (`MORALIS_API_KEY`) |

## NodeReal (BSC)

Two documented JSON-RPC methods on `https://bsc-mainnet.nodereal.io/v1/{key}`
(the host is pinned by settings; the key is appended to the path by the
transport and never stored in the URL setting):

- `nr_getTokenHolders(token, pageSize, "", topN)` — one page, `topN` sent
  explicitly (= `ATLAS_HOLDER_PAGE_SIZE`, capped at NodeReal's page maximum
  100; default 50). Balances are hex raw integers.
- `nr_getTokenHolderCount(token)` — the holder count, established separately.

Checked, not trusted: the JSON-RPC envelope must answer this request (version,
id, no error); rows must be well-formed addresses with uint256 balances, unique
and in non-increasing order; the count may not be smaller than the rows. The
result is `COMPLETE` only when the count equals the rows, otherwise a
`TOP_N_ONLY` prefix of at least ten rows; fewer is `INCOMPLETE_RESULT`. No
provider percentage or supply exists in the answer; the denominator is the
on-chain `totalSupply` ATLAS reads over RPC. No block is named, so the
observation is `RESPONSE_TIME` (received after both answers) — accepted by the
PAPER ATLAS policy only; nothing is invented to anchor it to a block.

**Cost:** 2 requests and 350 CU per holder read (300 + 50, per NodeReal's API
marketplace, which lists both methods for every plan including Free).

## Refusals

- A selected provider without its key refuses the configuration:
  `ATLAS_PROVIDER_KEY_MISSING` (the `--preflight` reason and the run refusal).
- With a PAPER run requested and ATLAS on, `--preflight` checks
  `ATLAS_HOLDER_SOURCES`: every chain with an on-chain source needs a holder
  source, else `ATLAS_HOLDER_SOURCE_NOT_CONFIGURED`.

## Provider exclusions and their reconciliation (Robinhood / Blockscout)

Blockscout removes the zero address from its holder list server-side and the
adapter declares that (`excluded_addresses=(0x0…0,)`); the declaration is never
dropped. ATLAS reconciles it:

- only an explicitly reconcilable address (`RECONCILABLE_EXCLUSIONS`: the zero
  address), only for a block-anchored holder state (`SOURCE_BLOCK`), and only
  at **exactly the provider's `snapshot_block`** — never latest;
- the read is ERC-20 `balanceOf(0x0)` over our RPC (`EvmRpcClient.balance_of`,
  fixed selector, one 32-byte answer or a contract failure);
- a successful read joins the rows before any share is computed, so a large
  zero-address holding counts in the raw top-1/5/10 like any holder; the
  burn-adjusted figure stays a separate measure and is only claimed from a
  complete set;
- recorded as `reconciled_exclusions` (address, balance, block, method
  `ERC20_BALANCE_OF_AT_SNAPSHOT_BLOCK`) beside `provider_excluded_addresses`.

Unresolved — and still `HOLDER_METRIC_UNDERSTATED` in risk readiness — is every
exclusion without such a reconciliation: no snapshot block, a failed or
malformed read, any other excluded address, a reconciliation at another block,
and all legacy evidence (which never carries the field; its stored bytes and
fingerprints are unchanged). Evidence and snapshot digests only change when a
reconciliation is present.
