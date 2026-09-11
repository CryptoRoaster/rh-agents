# Phase 2G: SIGNAL data enablement — real Farcaster observations via Neynar

Phase 2F built SIGNAL and deliberately gave it nothing to read. This phase
connects one real public source, and the whole design question is how to do that
without weakening a single boundary Phase 2F established in order to obtain more
posts.

Nothing about SIGNAL was redesigned. Normalization, identity, binding,
duplicate detection, repost semantics, concentration, the quality policy, the
model input and the semantic validator are all exactly as Phase 2F left them.
Phase 2G supplies observations and nothing else.

## Why Farcaster, and why only Farcaster

Phase 2F's provider research recommended Neynar first because its cast search was
the only candidate verified to return everything the deterministic layer needs: a
source timestamp, a stable numeric author identity, recast and reply counts,
thread parentage, date-filtered search and cursor pagination.

X, Reddit, Telegram and Discord are deliberately **not** added. One real provider
path should be validated end to end before provider semantics multiply — each one
brings its own identity model, its own coverage guarantees and its own terms, and
debugging three at once means debugging none of them properly.

## Verified provider contract

Read from Neynar's own documentation during this phase. Anything not verified is
marked as such rather than assumed.

| Concern | Verified answer |
| --- | --- |
| Endpoint | `GET /v2/farcaster/cast/search/` |
| Authentication | `x-api-key` header |
| `mode` | `literal` \| `semantic` \| `hybrid` — default `literal` |
| `sort_type` | `desc_chron` \| `chron` — default `desc_chron` |
| `limit` | 1–100, default 25 |
| `cursor` | opaque string |
| Other parameters | `author_fid`, `viewer_fid`, `channel_id`, `parent_url` |
| Query syntax | `+` AND, `\|` OR, `*` prefix, `"phrase"`, `()` grouping, `~n` fuzziness, `-` negation, `before:YYYY-MM-DD[THH:MM:SS]`, `after:YYYY-MM-DD[THH:MM:SS]` |
| Response envelope | `result.casts[]`, `result.next.cursor` (nullable) |
| Cast fields used | `hash`, `timestamp` (date-time), `author.fid`, `text`, `parent_hash` (nullable), `reactions.likes_count`, `reactions.recasts_count`, `replies.count` |
| Cast fields present and ignored | `embeds`, `channel`, `mentioned_profiles`, `author.username`, `author.display_name`, `author.score`, `author.experimental.neynar_user_score` |
| Rate limits | Free 600 RPM / 10 RPS per endpoint; Scale 1200 / 20; legacy Starter 300 / 5, Growth 600 / 10. Independent of credits |
| Credit cost | **Not published for this endpoint.** The credits page lists `user/search` at 10 and `channel/search` at 20; cast search does not appear. Treated as unknown and bounded by request budget rather than guessed |
| Exhaustiveness | **No completeness guarantee is documented.** See coverage below |

### Three provider knobs deliberately left off

Each gives up recall. Each would otherwise let the provider pre-select what
SIGNAL concludes.

* **`mode=literal`, not `semantic` or `hybrid`.** Relevance ranking is a sample
  someone else selected. Literal chronological search has a provenance we can
  state: the casts matching this string in this interval, newest first. A ranked
  set would quietly become a sentiment prior.
* **No `viewer_fid`.** A viewer personalizes results through one account's mutes
  and blocks. SIGNAL wants the unpersonalized public set, not what one wallet
  would see.
* **No provider-side spam filtering, and no use of `author.score`.** SIGNAL
  exists to measure duplication, author concentration, burstiness and campaign
  structure. A provider that removes the spam first removes exactly the evidence
  being measured. The score reaches nothing and filters nothing; there is a test
  asserting it does not even appear in a normalized observation.

`mode` and `sort_type` are sent explicitly even though both are the documented
defaults, because a default is a vendor's to change.

**No x402 or micropayment access.** RH Agents has no wallet, no signer and no
autonomous payment capability, and this phase introduces none. Standard API-key
authentication only.

## The chain-identity problem

This is the part that required real care, and it is the reason the phase needed
an audit before a line of adapter code.

A 20-byte EVM address is **chain-scoped**. The same deployer at the same nonce
reproduces the same address on every EVM chain, so occupying an address on a
second chain costs a scammer almost nothing. Farcaster search is not chain-aware.

So this inference is forbidden:

> We searched Farcaster for this TradeCase's contract address. This cast came
> back containing it. Therefore this cast is about our token on our chain.

It is circular — the query cannot be evidence for its own premise, because the
query is what produced the result. Phase 2F already enforces the far end of this:
`CONTRACT_ADDRESS_EXACT` requires `binding_chain`, and the collector refuses the
binding when it does not match the TradeCase. What Phase 2G had to get right is
never manufacturing that chain from the TradeCase.

### Chain context comes from the cast, or not at all

`signal-chain-context-v1` accepts exactly two deterministic sources.

**Allowlisted explorer links.** Host compared in full, never by suffix, plus an
address path segment naming the address in question:

| Host | Chain |
| --- | --- |
| `bscscan.com`, `www.bscscan.com` | `bsc` |
| `robinhoodchain.blockscout.com` | `robinhood` |

**A tiny closed alias set,** matched on whole words in prose only:

| Alias | Chain |
| --- | --- |
| `bnb smart chain`, `binance smart chain`, `bnb chain`, `bsc` | `bsc` |
| `robinhood chain` | `robinhood` |

Deliberately excluded: bare **`bnb`** (the asset, not the chain), bare
**`robinhood`** (the brokerage, the app, the company — overwhelmingly not the
chain) and **`rh`** (two letters that mean everything).

**URLs are stripped before alias matching.** This was a real defect caught by its
own test: `https://bsc-totally-real.example/token/0x…` was granting `bsc` context
because the alias appeared in the hostname. Anyone can register that domain. If a
link's spelling could grant chain context, the strongest half of an identity
decision would belong to whoever bought the name. Links now speak only through
the explorer allowlist, where the host is compared in full.

**Ambiguity resolves to nothing, in every direction.** Two supported chains named
in one cast, an explorer link contradicting the prose, or no signal at all are the
same answer: we do not know.

### `CONTRACT_ADDRESS_UNSCOPED`

One new binding basis, for the case that actually occurs: the cast contains this
token's exact address and gives no trustworthy chain context.

The alternatives were both wrong. Calling it `CONTRACT_ADDRESS_EXACT` would
invent a chain. Calling it `UNRESOLVED` would throw away a genuinely useful fact —
an exact 20-byte match is far stronger evidence than a ticker.

So it is **admissible and weak**: it enters the analysis, it is counted in
`weak_binding_count`, and it is not in `STRONG_BINDING_BASES`. A set resting
entirely on unscoped addresses raises `NO_STRONGLY_BOUND_OBSERVATIONS` and is
`DEGRADED`, never clean. The model can see the weakness and has no field with
which to promote it.

The invariant is enforced by the model itself: an unscoped binding must carry an
address and must **not** carry a chain, because carrying one would be a claim the
basis denies.

### `VERIFIED_PROJECT_LINK` stays closed

Phase 2F established that a provider cannot self-assert it, and Phase 2G changes
nothing. A cast containing a project URL, an embed pointing at one, or a provider
labelling a link official are none of them verification. No trusted project
identity registry exists, so the adapter never produces this basis at all, and
nothing in social data is allowed to create one.

## Query plan (`signal-neynar-query-v1`)

Two classes, bounded, and no more:

| Class | Query | Notes |
| --- | --- | --- |
| `CONTRACT_ADDRESS` | `"0x…"` as a literal phrase | The only class currently reachable |
| `SYMBOL` | `"$SYMBOL"` as a literal phrase | Implemented and escaped; not yet wired, because the repository's market identity carries no validated symbol. Wiring one is a Phase 2H decision, not something to fabricate here |

A **project-name query is deliberately absent.** Names are ambiguous and Phase 2F
has no deterministic binding rule that could consume one safely, so it would add
observations nothing could resolve. Recall is worth less than provenance.

**Query-language injection is prevented by construction.** Neynar's search
language gives meaning to `+ | * " ( ) ~ -` and to `before:`/`after:`, so a symbol
is only ever interpolated when it matches `^[A-Za-z0-9]{2,16}$`. A token named
`A|B` must not silently become a disjunction, and one named `after:2020` must not
rewrite the window it is being searched in. Ten such cases are tested.

**Discovery route is not binding evidence.** A cast found by the address query is
a cast containing a string. What it is about is decided from its own content.

## Time, coverage and what the evidence means

The window comes from Phase 2F policy (6 hours) and is sent as explicit UTC
`after:`/`before:` bounds — bounding the provider is better than fetching broadly
and filtering locally. **Every returned timestamp is re-validated anyway.** The
provider query is not authority; a cast outside the window is dropped and counted,
and there is a test in which the provider ignores the bounds entirely.

`timestamp` is the source event time and becomes `source_created_at`. Our fetch
time becomes `received_at`. A cast written an hour ago is an hour old however
recently it was collected, and freshness continues to depend only on the first.

**Coverage is a result set, not a census.** Neynar documents no exhaustiveness
guarantee for cast search, so SIGNAL evidence means *"analysis of the observation
set this bounded query plan collected"* — never *"all Farcaster sentiment"* and
certainly never "all public opinion". The bounded page budget makes that even more
true: a busy token's discussion is sampled, not enumerated.

## Normalization

| Phase 2F field | From Neynar | Why |
| --- | --- | --- |
| `observation_id` | `uuid5` over `FARCASTER` + cast `hash` | Namespaced. Post `42` exists on every platform |
| `source_native_id` | cast `hash`, format-validated | Provenance only, never grouped on |
| `author_id` | `author.fid` as a string | The numeric FID, never `username` (rentable, changes) or `display_name` (collides). `author_key` becomes `FARCASTER:<fid>` |
| `kind` | `REPLY` when `parent_hash` is set, else `ORIGINAL` | A reply is an authored position and is neither excluded nor treated as a share |
| `created_at` | `timestamp` | Source event time |
| `received_at` | trusted clock at fetch | Audit only |
| `engagement` | `likes_count`, `recasts_count`, `replies.count` | Attention metadata. Absent stays absent, never coerced to 0 |
| `content` | `text`, whitespace-collapsed, bounded | See truncation below |

**Recasts are never observations.** Farcaster search returns casts, not recast
events, so `recasts_count = 500` is one post that was amplified five hundred
times. No author identity is synthesized from a count — that would be inventing a
crowd — and no second endpoint is called to enumerate recasters.

**Truncation is visible.** A silently shortened post could read as a different
sentiment from the one written, so text beyond the observation bound is cut with
an explicit `…[truncated]` marker that a reviewer and the content fingerprint both
see. Provider text beyond 4096 characters is a shape we do not recognise and is
refused outright.

**Embeds are never dereferenced.** No browser, no crawler, no image fetch, no
OpenGraph lookup. Embed URLs are untrusted strings from strangers, and the only
safe thing to do with them is nothing — which also means social content has no
SSRF path at all.

Content fingerprinting is untouched: the adapter supplies bounded factual text and
Phase 2F's `signal-content-v1` computes every hash and cluster.

## Deduplication and precedence

The same cast can match several query classes. It is one post by one author
either way, so it becomes one observation keyed on the cast hash — no doubled
attention, no doubled author, no doubled sentiment sample.

Binding precedence is resolved from content, not from which search found it.

## Request and cost budget

| Bound | Value |
| --- | --- |
| Pages per query class | `SIGNAL_NEYNAR_MAX_PAGES` (default 2) |
| Casts per page | `SIGNAL_NEYNAR_PAGE_SIZE` (default 50, provider max 100) |
| HTTP requests per assessment | `max_pages × 2` query classes = 4 by default |
| Response bytes | 2 MB per response |
| Timeout | `SIGNAL_SOURCE_TIMEOUT_SECONDS` (default 10) |

Budget exhaustion is an explicit typed failure, never a quietly "complete"
result. A provider that always promises another page cannot spend an account
through this worker, and that case is tested with fresh cursors so the cycle
check cannot be what stops it.

Transport-level retries are deliberately absent. Phase 2B owns durable task
retries; a second retry layer would multiply into it — five transport attempts
inside three worker attempts is fifteen calls against the rate limit that was
already the problem. `429` maps to a retryable category and Phase 2B decides.

Pricing is documentation only and is never in runtime logic. Neynar advertises a
free developer tier; the exact cast-search credit cost is not published, so the
operational protection is the request budget above rather than a guessed number.

## Configuration

```
SIGNAL_SOCIAL_PROVIDER=disabled | neynar
NEYNAR_BASE_URL=https://api.neynar.com
NEYNAR_API_KEY=…                     # SecretStr
SIGNAL_SOURCE_TIMEOUT_SECONDS=10
SIGNAL_NEYNAR_PAGE_SIZE=50
SIGNAL_NEYNAR_MAX_PAGES=2
```

**Disabled by default, and a key alone selects nothing.** A credential in the
environment is not consent to spend credits. Selecting the provider still starts
no worker, makes no call at import or startup, and creates no background task —
Phase 2G wires a data source, not an autonomous runtime.

Selecting the provider without its key refuses to boot, which is louder and
cheaper than discovering it on the first safety-relevant read.

The base URL is validated as the documented origin with the host compared in
full: `api.neynar.com.evil.example` ends with the right characters and is a
different server. Credentials, query strings and fragments in the URL are
refused, redirects are not followed, and `trust_env` is off.

**There is deliberately no `fake` provider option.** The Phase 2F deterministic
source stays test-only and is reachable from no production path. If Neynar cannot
answer, SIGNAL is unavailable and the case waits — synthetic sentiment carrying a
real TradeCase forward is the one failure mode a fixture source could cause, and
the way to prevent it is for production to have no path to one.

## Workflow behaviour

Unchanged from Phase 2F, and now exercised with real provider failures:

* **Provider outage** → source `UNAVAILABLE` → evidence `UNAVAILABLE` with
  assessment `UNKNOWN` → case stays `EVIDENCE_PENDING`. No fabricated neutral
  sentiment, and no synthetic fallback.
* **Recovery** → a later valid observation set supersedes the old evidence
  through ordinary Phase 2A semantics and the evaluator re-runs. No manual
  transition.
* **Sentiment stays out of `risk_input_digest`.** Real data changes nothing about
  that boundary.
* **A SIGNAL reading that later goes stale** returns the case to
  `EVIDENCE_PENDING` through the evaluator while leaving any stored RiskBinding
  intact, exactly as Phase 2F proved.

### Future executor invariant

> An executor must require a **currently executable workflow state** and a
> **current final SENTINEL revalidation** — never the mere existence of an older
> RiskBinding. Workflow eligibility and risk-binding validity are different
> questions, and a required prerequisite that has since gone stale must not be
> able to hide behind a stored authorization.

Documented, not implemented. No executor exists and none is added here.

## Security boundary

* The SIGNAL worker receives `{lease, context, submit}` and no HTTP client, no
  Neynar client, no API key, no base URL and no raw JSON. Only the infrastructure
  adapter knows the provider exists.
* The reasoning provider still has no tools. There is no "search Farcaster" tool
  and no model-reachable network of any kind; provider access lives entirely
  outside the model.
* One fixed validated origin, no arbitrary URL, no embed dereference, no SSRF
  path from social content.
* The key is a `SecretStr`, travels in a header, and is never logged, printed,
  persisted, returned by an API, put into evidence or included in an exception —
  failures carry a category name only.
* No raw provider response is persisted. Evidence keeps metrics, identifiers,
  hashes and provenance, exactly as Phase 2F established.
* No wallet, signer, executor, broadcast, ledger write, SENTINEL mutation or
  RiskBinding creation. No public mutation route. No Docker.

## Provider status matrix

| Property | Status |
| --- | --- |
| Adapter | **IMPLEMENTED** |
| Official API support | **VERIFIED** against current Neynar documentation |
| Credentials | **REQUIRED** |
| Live authenticated smoke | **NOT RUN** — opt-in only; no key and no opt-in flag were present |
| Operational default | **DISABLED** |
| Search coverage | Bounded query-plan result set. **Not** a complete sentiment census; no exhaustiveness guarantee is documented |
| Chain-specific identity | Requires deterministic chain context from the cast itself; an unscoped address is admitted as a weak binding |
| Credit cost per assessment | Bounded at 4 requests. Per-request credit cost for cast search is not published |

An optional live smoke exists (`RH_AGENTS_LIVE_SIGNAL_SMOKE=1` **plus**
`NEYNAR_API_KEY`) and reads one bounded page against a harmless literal query. It
reports counts and schema validity only — never cast text, never a key — and
invokes no model. A TradeCase-bound live social smoke waits until a deliberate
test asset exists; no financial action belongs in a test suite.

## Persistence

**No migration.** No social archive, no corpus storage. The existing evidence
payload remains sufficient. Migrations `0001`–`0006` are untouched and `0006`
remains head.

## What Phase 2G does not implement

No X, Reddit, Telegram or Discord. No scraping of any kind, and no fallback to
one. No private channels, groups or user sessions. No x402, wallet or payment
path. No embed dereferencing. No provider-side filtering. No trusted project
identity registry. No symbol wiring. No worker launcher. No executor, and no
VECTOR, PULSE, ANCHOR, FUSE or COMMANDER.
