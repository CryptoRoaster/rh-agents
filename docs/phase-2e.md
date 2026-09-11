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
| Ordering | **Proven** descending — see below |
| Filtering | The zero address is removed by the provider — see below |

#### The ordering guarantee, and what it actually rests on

A cursor keyed on `value` proves only that *an* ordered paging scheme exists. It
does not prove the direction, that the first page holds the globally largest
balances, or that no unseen page hides a larger holder. Those are the properties
a `TOP_N_ONLY` prefix depends on, so they are established from the endpoint's own
implementation rather than inferred:

| Claim | Basis |
| --- | --- |
| Sorted by balance, descending | `order_by([tb], desc: :value, desc: :address_hash)` in `CurrentTokenBalance.token_holders_ordered_by_value_query_without_address_preload/2`, reached from the v2 controller through `Chain.fetch_token_holders_from_token_hash/2` |
| Documented as such | The endpoint's own OpenAPI operation: *"List addresses holding a specific token sorted by balance"* |
| No later page can hold a larger balance | The keyset predicate `tb.value < ^value or (tb.value == ^value and tb.address_hash < ^address_hash)` in `Chain.page_token_balances/2`. Page *n+1* is restricted to rows strictly below the last row of page *n* in the same total order |
| Sort key | `(value, address_hash)`, both descending |
| Tie semantics | Equal `value` breaks on **descending** `address_hash` |

Two distinct things are therefore kept apart, and neither is allowed to stand in
for the other:

* **`PROVIDER_ORDER_GUARANTEE`** — the contract above. It is what makes a prefix
  a *global* top-N.
* **`OBSERVED_PAGE_ORDER_VALIDATION`** — our own check that every row actually
  arrived in descending order, across page boundaries. It can catch a broken or
  modified deployment. It can never establish the global property, because a
  locally tidy `100, 90, 80 | 70, 60` says nothing about an unrequested page that
  holds `500`.

Our canonical tie-break is *ascending* address, which is **not** Blockscout's and
is never presented as it. It is normalization only — so that identical input
always yields the same top-N and the same digest — and it cannot move a metric:
tied rows hold equal balances, so whichever of them lands in the top-N leaves
every top-N sum identical.

This basis is the open-source implementation of the endpoint and its published
schema. It has not been re-verified against an authenticated live PRO response;
see the support matrix.

### BNB Smart Chain (56) — Moralis for holders, Etherscan V2 for creation

| Concern | Answer |
| --- | --- |
| Holders | `GET /erc20/{address}/owners?chain=0x38&order=DESC` |
| Auth | `X-API-Key` header |
| Pagination | Opaque `cursor` |
| Ordering | **Documented**, not proven: `order` is a documented request parameter taking `ASC` or `DESC`, and the endpoint documents owners sorted by balance |
| Filtering | None documented |

`order=DESC` is sent explicitly on the first page **and** on every continued
page. The documented default is already `DESC`; relying on it would make the
prefix guarantee depend on a vendor's freedom to change a default, so the
parameter we depend on is the parameter we send. There is a test on the request
construction of both pages.

The cursor is opaque, so unlike Robinhood there is no inspectable predicate
proving that a later page cannot hold a larger balance. The global top-prefix
therefore rests on the provider's stated ordering contract rather than on
something we can verify from the outside. Observed cross-page order is validated
regardless, with the same caveat as above: it is validation, not the guarantee.
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
* **We exclude nothing from the raw metric** — not liquidity pools, not burn
  addresses, not the deployer. A dominant pool stays visible as a dominant pool.
  What a *provider* removes before we ever see it is a different matter, is
  recorded, and is covered below.
* A provider's own percentage is ignored entirely. If it disagrees with
  arithmetic, arithmetic wins; there is nothing for a vendor to move.

### Completeness

`HolderCompleteness` is explicit, and policy names which values satisfy the
domain:

| Value | Meaning |
| --- | --- |
| `COMPLETE` | Every holder row **the provider exposes** was retrieved |
| `TOP_N_ONLY` | A provably balance-ordered prefix was retrieved — enough for a top-10 against a known supply, and nothing more |
| `UNKNOWN` | Neither could be established. No metric may be derived, ever |

A prefix shorter than ten rows cannot support a top-ten share and is refused.
Holder count is a separate fact: it comes from Blockscout's `holders_count` and
is simply absent on BSC. It is never inferred from a page length.

Completeness is about **our paging**, not about the provider's own filtering.
The two are deliberately separate fields, because merging them would let a
filtered list pass as a full one.

### Provider-side filtering

Blockscout's holder query carries `where: address_hash != burn_address_hash`, and
that constant is the zero address — `0x…dead` is a different constant and is
*not* filtered. So `COMPLETE` from Robinhood means "every holder except the zero
address", and no amount of paging will ever return that row.

That is recorded rather than inferred:

* `HolderFactsSourceResult.excluded_addresses` and `HolderFacts.excluded_addresses`
  name what the provider removed. Blockscout declares the zero address; Moralis
  declares nothing.
* The field is part of the fact document the snapshot digest is taken over, so a
  provider that silently changes its filtering changes the fingerprint instead of
  passing unnoticed.
* The **burn adjustment is withheld** whenever a burn address is among the
  exclusions, exactly as it is withheld for a prefix. A burn total computed from
  rows that could never contain the sink is a lower bound, and a lower bound used
  as a denominator adjustment *understates* concentration.

The raw `top1`/`top5`/`top10` are unchanged by this: a row that never arrived
cannot be added back, and nothing else is removed. Their honest reading on
Robinhood is "the largest holders other than the zero address, measured against
full on-chain supply".

### Burn handling

The canonical burn set stays exactly what ATLAS already recognised — the zero
address and `0x…dead`. Nothing is treated as burned because its hex looks like a
sink. `burned_raw`, `burned_share` and `top10_share_excluding_burn` are computed
**only** when the holder set is `COMPLETE` **and** no burn address sits in
`excluded_addresses`, because in either case the burned amount is a lower bound,
and a lower bound used as a denominator adjustment would understate
concentration. On Robinhood that second condition is not met today, so the
adjusted figures are absent there and present on BSC. The adjusted figure, when
it exists, is always reported beside the raw one, never instead of it.

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

### Four different times, never one field

| Kind | What it proves | Where it appears |
| --- | --- | --- |
| **Block time** | The chain produced this state at T. Authoritative | `ChainSnapshot.block_timestamp`; the Blockscout indexer head's chain timestamp |
| **Indexer snapshot time** | The indexer's view is as of T | Not offered by either holder provider today |
| **Provider generated time** | The provider built this representation at T | Not offered by either holder provider today |
| **Response received time** | *We received this representation* at T | `HolderObservationBasis.RESPONSE_TIME`; `ChainSnapshot.observed_at` |

The last one is the trap. "We received this at 10:20" does not mean "the indexed
holder state is from 10:20". Nothing in a response receipt can distinguish a
current indexer from one that is three hours behind.

| Basis | Provider | Anchor | Reading |
| --- | --- | --- | --- |
| `SOURCE_BLOCK` | Blockscout | Indexer head block + its **chain** timestamp | Source observation time |
| `RESPONSE_TIME` | Moralis | The moment the response was **received** | Receipt only. The API names no block, no block hash and no indexer timestamp |

`RESPONSE_TIME` is materially weaker assurance. It is labelled as such on the
fact, carried into the snapshot digest, and documented here rather than dressed
up as parity. Nothing fabricates a block or a snapshot time to fill the gap: on
BSC `snapshot_block` is `None` and stays `None`.

### Re-fetching never renews a fact

A second HTTP receipt of the same logical snapshot yields a second receipt and
nothing else:

* Where the source names an authoritative time, that time is what freshness is
  judged by. Fetching again at 10:20 cannot reset a 10:00 observation, and
  `oldest_source_observation` takes the **minimum** across contributing sources,
  so one current source never rescues a stale one.
* Where the source names only a receipt — Moralis — a test can prove what the
  basis is *called* and that no block is invented, and that is the honest limit
  of what any test can prove here. It cannot prove indexer freshness, because the
  provider supplies nothing that would let anyone prove it.

### Skew: two operands that are not epistemically equal

`max_source_skew` (5 minutes) compares the pinned block's **chain timestamp**
against the holder anchor. Against a `SOURCE_BLOCK` holder fact that is a genuine
source-to-source skew. Against a `RESPONSE_TIME` holder fact it is not — it
bounds only how far the pinned block lags the moment of the answer, and it cannot
detect a lagging indexer at all.

That asymmetry is handled by naming it, not by hiding it in one timestamp field:

* `HolderObservationBasis` on the fact says which reading applies; the number
  alone never does.
* The weaker source is gated on **assurance**, through
  `AtlasPolicy.accepted_holder_observation_bases`, not through the skew number.
* `context.source_skew()` uses the same definition the policy does — block time,
  never fetch time — and its docstring carries the same warning.

The Blockscout indexer head is read **before** the holder pages, so recorded
provenance can only under-claim freshness. The distance to the pinned chain block
is recorded as `holder_block_delta` — pinned block minus holder snapshot block,
so it is *negative* in the ordinary case, because the pinned block trails the
chain head by the confirmation lag while an indexer tracks the head. It is a
signed distance, never a one-directional "lag". Time is what policy judges: a
Blockscout indexer far behind the chain produces `SNAPSHOT_SKEW_EXCEEDED` and
`SNAPSHOT_STALE`, and a fresh HTTP round-trip cannot rescue it. There is a test
that pins exactly that. This detection exists **because** the source names a
block; it has no equivalent on BSC.

## Policy: what now satisfies HOLDERS

`atlas-policy-v2` states the minimum facts explicitly instead of trusting a
status flag. The holder domain is satisfied when **all** of:

* the source answered `AVAILABLE`;
* the response identified the same chain and the same token;
* completeness is `COMPLETE` or `TOP_N_ONLY` (never `UNKNOWN`);
* the observation basis is one `accepted_holder_observation_bases` names;
* a top-10 share exists, computed against on-chain total supply;
* the observation is within `snapshot_validity` and `max_source_skew`.

### The PAPER-mode acceptance decision

`atlas-policy-v2` accepts **both** `SOURCE_BLOCK` and `RESPONSE_TIME`. For PAPER
evaluation, a documented current-owner endpoint, a valid current response, a
known provider identity and a bounded response age are enough to satisfy the
holder-data prerequisite with `provenance_assurance = RESPONSE_TIME_ONLY`.

This is a decision, not an oversight, and it is written as one field rather than
left implicit in what a vendor happens to return. What it buys is BSC coverage in
paper mode. What it does not buy is parity: a receipt-anchored fact is strictly
weaker than a block-pinned one and is never given the same assurance level.

### PRE-LIVE release blocker

> Before `LIVE_AUTONOMOUS` execution may be enabled, safety-critical holder data
> must carry provenance sufficient to detect material indexer lag.
> `RESPONSE_RECEIVED_TIME` alone does **not** qualify. A successor policy must
> drop `RESPONSE_TIME` from `accepted_holder_observation_bases` — unless a
> provider contract offers an independently trustworthy current-state freshness
> guarantee that is reviewed and deliberately approved on its own merits.

Concretely, with today's providers that means Robinhood/Blockscout would remain
live-eligible on this axis and BSC/Moralis would not, until BSC gains a
block-pinned or indexer-timestamped holder source.

This is a documented invariant only. Live mode is not implemented, no
`LIVE_AUTONOMOUS` setting was touched, and the policy field is enforced by tests
in both directions: the PAPER policy accepts a receipt anchor, and a policy that
demands block provenance rejects the same fact as `HOLDER_FACTS_UNAVAILABLE`
while its availability stays `AVAILABLE` — the answer arrived, the assurance was
short.

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

Origin facts may be `AVAILABLE` while `verification` is `UNVERIFIED`, and that is
deliberate under the current policy: ORIGIN is **not** a required domain, so the
verification state is carried as an explicit fact for a reader rather than folded
into availability. The moment a contradiction is measured — a receipt naming a
different contract — the domain goes unavailable and fails closed. If ORIGIN ever
becomes required, the cross-check must become a precondition for `AVAILABLE`, not
a label beside it.

Creator, factory and "creator is a contract" are **facts**. Developer-wallet
clustering is an **inference** and is deliberately not implemented here.

## Provider support matrix

One word cannot carry "adapter written", "provider documents this chain",
"credentials configured", "an authenticated call actually succeeded" and "it is
working right now". They are different claims with different failure modes, so
they get different columns.

| State | Meaning |
| --- | --- |
| `IMPLEMENTED` | The adapter exists and is proven against fixtures of the real schema |
| `DOCUMENTED_SUPPORTED` | The provider's own documentation or source says this chain and endpoint are served |
| `LIVE_VERIFIED` | An authenticated call to that provider for that chain has actually succeeded here |
| `REQUIRES_CREDENTIALS` | An account and key are needed before any live call is possible |
| `UNAVAILABLE` | Not served, or not implemented |

### Fact capability vs operational verification

| Fact domain | Chain | Provider | Implemented | Documented for chain | Live verified | Credentials | Provenance quality |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Holder concentration | Robinhood 4663 | Blockscout PRO | YES | YES — Blockscout documents full PRO integration for Robinhood Chain at `https://api.blockscout.com/4663/api/v2`; the holders endpoint and its ordering come from the v2 implementation and schema | **NO** — no authenticated PRO call has been made | YES | `INDEXER_HEAD` block + chain time (`SOURCE_BLOCK`) |
| Holder concentration | BSC 56 | Moralis | YES | YES — `bsc` / `0x38` and `order` (`ASC`/`DESC`, default `DESC`) are documented on the owners endpoint | **NO** | YES | `RESPONSE_ONLY` (`RESPONSE_TIME`) |
| Holder count | Robinhood 4663 | Blockscout `holders_count` | YES | YES | NO | YES | Same as holders; excludes the zero address |
| Holder count | BSC 56 | — | — | UNAVAILABLE | — | — | — |
| Complete holder set | both | as above | YES | PARTIAL by design — bounded paging, `TOP_N_ONLY` beyond the budget | NO | YES | Same as holders |
| Contract creator | Robinhood 4663 | Blockscout `getcontractcreation` | YES | YES | NO | YES | Creation block + tx |
| Contract creator | BSC 56 | Etherscan V2 `chainid=56` | YES | YES | NO | YES | Creation block + tx |
| Developer-wallet relationships | both | — | **NO** | — | — | — | Inference, not a fact. Deliberately not implemented |

**No holder or origin provider is `LIVE_VERIFIED`.** Fixtures prove adapter
correctness against the real response schemas; they prove nothing about an
authenticated account. For Robinhood specifically: a `401`/`402` from
`api.blockscout.com` shows the route reached an authentication boundary, which is
not the same as proving data is served for chain 4663 once past it. The
documentation claim above is what carries that, and the status stays
**IMPLEMENTED / REQUIRES CREDENTIAL VERIFICATION**.

### Facts read over our own RPC

These do not depend on a third-party vendor at all and are verified by the
existing RPC path.

| Fact | Robinhood (4663) | BSC (56) | Source | Snapshot provenance |
| --- | --- | --- | --- | --- |
| Contract code presence | AVAILABLE | AVAILABLE | managed EVM RPC `eth_getCode` | pinned safe block |
| ERC-20 `decimals()` | AVAILABLE | AVAILABLE | managed EVM RPC `eth_call` | pinned safe block |
| ERC-20 `totalSupply()` | AVAILABLE | AVAILABLE | managed EVM RPC `eth_call` | pinned safe block |
| EIP-1967 proxy / admin slots | AVAILABLE | AVAILABLE | managed EVM RPC `eth_getStorageAt` | pinned safe block |
| Creation receipt confirmation | AVAILABLE | AVAILABLE | managed EVM RPC `eth_getTransactionReceipt` | creation tx |

Runtime evidence status — `AVAILABLE` / `UNAVAILABLE` / `UNKNOWN` on a fact, and
`CLEAR` / `BLOCKED` / `INSUFFICIENT_DATA` on a verdict — is a separate axis from
all of the above and is computed per assessment from what actually answered.

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

Provider-semantics tests specifically:

* **Ordering** — an out-of-order page is refused on both vendors, and a second
  page holding a *larger* balance than the first is refused (cross-page
  monotonicity), so the observed-order validation is exercised in both the
  within-page and the across-page shape.
* **Filtering** — Blockscout declares the zero address as excluded, the exclusion
  reaches the fact and the digest document, and the burn adjustment is withheld
  on a `COMPLETE` set because of it; a vendor that filters nothing still reports
  its adjustment.
* **Request construction** — `order=DESC` and `chain=0x38` on the first *and*
  continued Moralis page; the Blockscout key in a header and never in a URL; the
  request budget scaling with the configured page budget.
* **Provenance** — a second receipt of the same Moralis snapshot twenty minutes
  later stays `RESPONSE_TIME` with `snapshot_block` still `None`; no block and no
  indexer timestamp is fabricated.
* **Assurance gating** — the PAPER policy accepts a receipt anchor; a policy
  demanding block provenance rejects the same fact as a data gap; a policy
  accepting no basis at all refuses to construct.

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
