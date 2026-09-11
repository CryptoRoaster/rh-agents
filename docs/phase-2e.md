# Phase 2E: ATLAS data enablement — holder intelligence and contract origin

Phase 2D shipped ATLAS deliberately unable to reach CLEAR: holder intelligence is
a required fact domain and no holder provider had been verified, so every real
assessment ended in `INSUFFICIENT_DATA` and the case blocked. That was the
correct fail-closed behaviour, and it was also an operational dead end.

Phase 2E resolves it the only acceptable way: by connecting **verified** sources,
not by weakening the policy. The holder domain is still required, the
concentration blocker is still switched off, and a deployment with no provider
configured behaves exactly as Phase 2D did.

## What changed in one line

ATLAS can now establish holder distribution and contract creation facts for both
supported chains, with provenance, bounded cost and exact arithmetic — and the
authority model is untouched.

## Provider research

Candidates were evaluated against live documentation and, where possible, live
responses. Nothing was selected from a summary.

| Candidate | Chain | Verdict |
| --- | --- | --- |
| Blockscout PRO API (`api.blockscout.com`) | Robinhood 4663 | **Selected** for holders and origin |
| Robinhood public explorer host (`robinhoodchain.blockscout.com`) | Robinhood 4663 | **Rejected** — see below |
| Moralis Data API (`deep-index.moralis.io`) | BSC 56 | **Selected** for holders |
| Etherscan V2 (`api.etherscan.io`) | BSC 56 | **Selected** for contract creation only |
| Etherscan V2 `tokenholderlist` | BSC 56 | **Rejected** — paid tier, and no documented ordering guarantee, so a top-N prefix cannot be proven |
| GoldRush / Covalent `token_holders_v2` | BSC 56 | **Rejected** — documents that burn addresses are excluded from results, which silently alters a raw distribution metric; ordering is undocumented |
| GoldRush / Covalent | Robinhood 4663 | **Rejected** — Robinhood is a "frontier" chain there with no token-holder endpoint |
| Blockscout | BSC 56 | **Not applicable** — Blockscout does not index BNB Smart Chain |

### Why the public Robinhood explorer host is not used

Robinhood's own documentation names `robinhoodchain.blockscout.com` as the chain
explorer, and its UI shows holder lists. Its JSON API answers an ordinary
server-side client with `HTTP 403` and `cf-mitigated: challenge` — an interactive
challenge page — on every path tested, including `/api/v2/stats`, the
Etherscan-compatible `/api` and `/api-docs`. Requests only succeed when they
carry a browser's User-Agent and client hints.

That is a deliberate access control, and satisfying it would mean impersonating a
browser. It is also a bad engineering dependency for a safety-critical fact: an
anti-bot ruleset can tighten at any time, and the holder domain would fail
without any change on our side. The credentialed `api.blockscout.com` host is the
documented, supported path for programmatic access and answers honest clients
cleanly, so that is what the adapter uses.

The transport still refuses to interpret an HTML body as data, and there is a
test for exactly that challenge page.

## Selected sources

### Robinhood Chain (4663) — Blockscout PRO API

| Concern | Answer |
| --- | --- |
| Holders | `GET /4663/api/v2/tokens/{address}/holders` |
| Token metadata | `GET /4663/api/v2/tokens/{address}` — `decimals`, `holders_count`, `total_supply`, `type` |
| Indexer head | `GET /4663/api/v2/main-page/blocks` — `height` and chain `timestamp` |
| Creation | `GET /v2/api?chain_id=4663&module=contract&action=getcontractcreation` |
| Auth | `Authorization: Bearer proapi_…` — a header, so the key never enters a URL |
| Pagination | Keyset, `next_page_params` = `{value, address_hash, items_count}` |
| Ordering | Guaranteed structurally: a cursor keyed on `value` can only page a balance-ordered set. Verified on every page regardless |

### BNB Smart Chain (56) — Moralis for holders, Etherscan V2 for creation

| Concern | Answer |
| --- | --- |
| Holders | `GET /erc20/{address}/owners?chain=0x38&order=DESC` |
| Auth | `X-API-Key` header |
| Pagination | Opaque `cursor` |
| Ordering | `order=DESC` is a documented request parameter. Verified on every page regardless |
| Creation | `GET /v2/api?chainid=56&module=contract&action=getcontractcreation` (Etherscan V2) |

Etherscan requires its key in the query string. The transport never logs a URL,
the httpx and httpcore loggers are filtered at construction, and a typed failure
carries a category only — so the key stays out of logs, tracebacks, evidence and
API responses. There is a test for that.

Blockscout and Etherscan return the *same* `getcontractcreation` shape, so one
strict parser serves both chains and they cannot drift apart in how a creator is
read.

## Holder model

A provider supplies **raw rows and provenance**. It never supplies a
concentration, because the denominator is on-chain `totalSupply()` read at the
pinned block, which the deterministic collector owns.

* Rows are **sorted by us**, descending by balance, tie-broken on the canonical
  lowercase address. Provider ordering is verified but never relied on.
* A duplicated holder address is a refusal, never a sum.
* `top1`, `top5` and `top10` are exact Decimal ratios over the full supply. No
  float appears anywhere, and uint256-scale balances stay exact integers.
* **Nothing is excluded from the raw metric** — not liquidity pools, not burn
  addresses, not the deployer. A dominant pool stays visible as a dominant pool.
* A provider's own percentage is ignored entirely. If it disagrees with
  arithmetic, arithmetic wins; there is nothing for a vendor to move.

### Completeness

`HolderCompleteness` is explicit, and policy names which values satisfy the
domain:

| Value | Meaning |
| --- | --- |
| `COMPLETE` | Every holder row was retrieved |
| `TOP_N_ONLY` | A provably balance-ordered prefix was retrieved — enough for a top-10 against a known supply, and nothing more |
| `UNKNOWN` | Neither could be established. No metric may be derived, ever |

A prefix shorter than ten rows cannot support a top-ten share and is refused.
Holder count is a separate fact: it comes from Blockscout's `holders_count` and
is simply absent on BSC. It is never inferred from a page length.

### Burn handling

The canonical burn set stays exactly what ATLAS already recognised — the zero
address and `0x…dead`. Nothing is treated as burned because its hex looks like a
sink. `burned_raw`, `burned_share` and `top10_share_excluding_burn` are computed
**only** when the holder set is `COMPLETE`, because from a prefix the burned
amount is a lower bound, and a lower bound used as a denominator adjustment would
understate concentration. The adjusted figure is always reported beside the raw
one, never instead of it.

### Reconciliation with on-chain supply

Sum and supply come from different moments, so equality is not required and no
percentage tolerance is invented. Two conditions are exact impossibilities and
fail closed:

* holders collectively holding **more** than the contract says exists;
* a provider reporting a **zero** total supply while the chain reports a positive
  one.

Both yield `SUPPLY_INCONSISTENT` and an unavailable holder domain.

## Freshness and provenance

Phase 2D anchored freshness to what a source *observed*, never to when we
fetched. Phase 2E keeps that and extends it per provider:

| Basis | Provider | Anchor |
| --- | --- | --- |
| `SOURCE_BLOCK` | Blockscout | The indexer head block and its **chain** timestamp |
| `RESPONSE_TIME` | Moralis | The moment of the response — the API names no block |

`RESPONSE_TIME` is materially weaker assurance. It is labelled as such on the
fact, carried into the snapshot digest, and documented here rather than dressed
up as parity.

The Blockscout indexer head is read **before** the holder pages, so recorded
provenance can only under-claim freshness. The distance to the pinned chain block
is recorded as `holder_block_delta` — pinned block minus holder snapshot block,
so it is *negative* in the ordinary case, because the pinned block trails the
chain head by the confirmation lag while an indexer tracks the head. It is a
signed distance, never a one-directional "lag". Time is what policy judges:
the existing `max_source_skew` (5 minutes) compares the pinned block's chain
timestamp against the holder observation. An indexer far behind the chain
therefore produces `SNAPSHOT_SKEW_EXCEEDED` and `SNAPSHOT_STALE`, and a fresh
HTTP round-trip cannot rescue it. There is a test that pins exactly that.

## Policy: what now satisfies HOLDERS

`atlas-policy-v2` states the minimum facts explicitly instead of trusting a
status flag. The holder domain is satisfied when **all** of:

* the source answered `AVAILABLE`;
* the response identified the same chain and the same token;
* completeness is `COMPLETE` or `TOP_N_ONLY` (never `UNKNOWN`);
* a top-10 share exists, computed against on-chain total supply;
* the observation is within `snapshot_validity` and `max_source_skew`.

`max_top10_concentration` **stays `None`**. Data became available; a threshold
did not. A concentration limit is a product decision with real financial meaning,
and enabling one merely because the data now exists would invent a number nobody
chose.

**Read this carefully:** with the threshold disabled, a `PASS` on the holder
domain means *the data-quality prerequisite was satisfied*. It does **not** mean
the holder distribution was judged safe. CLEAR means every required fact was
established, fresh and internally consistent, and no configured deterministic
blocker fired. It is not a claim that the token is economically safe, and a
switched-off threshold must never be read as a passed one.

## Contract origin

`OriginFacts` now carries the creation transaction, the factory address when the
deployment came from one, whether the creator address is itself code, and an
explicit `OriginVerification`:

| Value | Meaning |
| --- | --- |
| `RECEIPT_CONFIRMED` | The creation receipt's `contractAddress` equals the token |
| `UNVERIFIED` | The claim was not checked, or the check could not be made |
| `NOT_ATTEMPTED` | No verifier was configured |

A provider returning well-formed JSON is never verification. A creation
transaction that created *some other* contract makes the origin domain
unavailable rather than attributing another token's deployer to this one.

Creator, factory and "creator is a contract" are **facts**. Developer-wallet
clustering is an **inference** and is deliberately not implemented here.

## Capability matrix

| Fact | Robinhood (4663) | BSC (56) | Source | Snapshot provenance | Cost / config |
| --- | --- | --- | --- | --- | --- |
| Contract code presence | AVAILABLE | AVAILABLE | managed EVM RPC `eth_getCode` | pinned safe block | own RPC |
| ERC-20 `decimals()` | AVAILABLE | AVAILABLE | managed EVM RPC `eth_call` | pinned safe block | own RPC |
| ERC-20 `totalSupply()` | AVAILABLE | AVAILABLE | managed EVM RPC `eth_call` | pinned safe block | own RPC |
| EIP-1967 proxy / admin slots | AVAILABLE | AVAILABLE | managed EVM RPC `eth_getStorageAt` | pinned safe block | own RPC |
| Holder concentration (top 1/5/10) | AVAILABLE | AVAILABLE | Blockscout PRO / Moralis | indexer head block + chain time / response time | free tier, key required |
| Holder count | AVAILABLE | UNAVAILABLE | Blockscout `holders_count` | same as holders | free tier, key required |
| Complete holder set | PARTIAL | PARTIAL | bounded paging; `TOP_N_ONLY` beyond the budget | same as holders | free tier, key required |
| Contract creator / deployer | AVAILABLE | AVAILABLE | Blockscout / Etherscan V2 `getcontractcreation` | creation block + tx, receipt-confirmed | free tier, key required |
| Creation receipt confirmation | AVAILABLE | AVAILABLE | managed EVM RPC `eth_getTransactionReceipt` | creation tx | own RPC |
| Developer-wallet relationships | **UNAVAILABLE** | **UNAVAILABLE** | not implemented — inference, not a fact | — | — |

PARTIAL is honest: one page is fetched by default, so a token with more holders
than the page budget yields a proven ordered prefix, not the whole universe.

## Cost and request budget

| Provider | Plan | Limit | Requests per ATLAS assessment |
| --- | --- | --- | --- |
| Blockscout PRO | Free tier available; paid tiers exist | 100K credits/day, 5 RPS on free | 3 for holders (metadata, indexer head, one page), 1 for origin |
| Moralis | Free tier available | 40K compute units/day, 50 CU per holders call | 1 for holders |
| Etherscan V2 | Free tier for `getcontractcreation` | standard free-tier rate limit | 1 for origin |

No paid dependency is hidden: **every provider requires an account and an API
key, including on its free tier.** Paging is bounded by `ATLAS_HOLDER_MAX_PAGES`
(default 1) and the transport enforces a hard request budget derived from that
page budget, so one assessment can never become a crawl — and raising the page
budget raises the request ceiling with it rather than silently truncating.

## Configuration

Every provider defaults to `disabled`. A key sitting in the environment is not
consent to spend, and selecting a provider still starts no worker.

```
ATLAS_RH_HOLDER_PROVIDER=disabled | blockscout
ATLAS_BSC_HOLDER_PROVIDER=disabled | moralis
ATLAS_RH_ORIGIN_PROVIDER=disabled | blockscout
ATLAS_BSC_ORIGIN_PROVIDER=disabled | etherscan
BLOCKSCOUT_BASE_URL=https://api.blockscout.com
BLOCKSCOUT_API_KEY=…            # SecretStr
MORALIS_BASE_URL=https://deep-index.moralis.io/api/v2.2
MORALIS_API_KEY=…               # SecretStr
ETHERSCAN_BASE_URL=https://api.etherscan.io
ETHERSCAN_API_KEY=…             # SecretStr
ATLAS_SOURCE_TIMEOUT_SECONDS=10
ATLAS_HOLDER_PAGE_SIZE=50       # Moralis only; Blockscout fixes its page at 50
ATLAS_HOLDER_MAX_PAGES=1
```

Selecting a provider without its key refuses to boot, rather than failing on the
first safety-critical read. Base URLs must be plain HTTPS origins with no
credentials, query string or fragment — the adapter can reach no other host.

Routing is per chain and explicit. A chain with no configured provider is simply
absent from the table and answers `NOT_CONFIGURED`; it never borrows another
chain's source, because address equality means nothing across chains.

## Security boundary

* A worker still receives exactly `{lease, context, submit}`. The read port has
  one method, `onchain_context`.
* No HTTP client, base URL or credential is ever handed to a worker. Adapters are
  constructed by the deterministic collector from validated configuration.
* No URL is ever taken from a model, a user, or a provider response. Only the
  documented keyset parameters are echoed back into a request, so a response
  cannot inject a query parameter or redirect the next call.
* Redirects are refused and ambient proxy configuration is ignored, so a
  credential cannot be carried to a host nobody configured.
* Responses are bounded in time, size and count; non-JSON content types are
  refused; JSON with duplicate keys or non-finite numbers is refused.
* Failures are typed categories. No URL, query string or response body reaches a
  log, a traceback, an evidence record or an API response.
* No wallet, signer, executor or broadcast path exists, and none was added. No
  Docker, no container.

## Persistence

**No migration.** The new facts and provenance fit the existing evidence JSONB
payload. Migrations `0001`–`0006` are untouched and `0006` remains head.

## Testing

Every automated test runs without a network. Provider behaviour is proven against
fixtures of the real response schemas, including the multi-page, repeated-cursor,
empty-page, oversized-page, duplicate-holder, malformed-row, wrong-token,
wrong-type and HTML-challenge cases.

Two opt-in live smokes exist and are skipped by default:

* `RH_AGENTS_LIVE_ONCHAIN_SMOKE=1` reads the documented public Robinhood Chain
  RPC — credential-free, read-only, no charge.
* `RH_AGENTS_LIVE_HOLDER_SMOKE=1` plus `BLOCKSCOUT_API_KEY` reads real holder and
  creation data. It consumes provider credits, so a key alone does not enable it.

## What Phase 2E does not implement

No developer-wallet clustering. No own transfer-log indexer. No provider voting
or majority quorum — one trusted source per fact domain and chain, with source
identity preserved so redundancy remains possible later. No re-assessment
trigger. No concentration threshold. No worker launcher: ATLAS and ORBIT still
start only when a future phase introduces a worker entrypoint. No SIGNAL, VECTOR,
PULSE, ANCHOR, FUSE, COMMANDER or EXECUTOR.
