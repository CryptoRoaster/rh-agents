# Early entries and the V4 pool census: offline analysis

Issue #77 (with #40). Analysis only: no code, policy, threshold or runtime
change. Every figure is tagged:

- **MEASURED**: read from stored evidence (the holder-source probe of
  2026-10-14, the discovery shadow database, the docs' recorded live case).
- **DERIVED**: follows from the code at `main` `9234fbf`.
- **ESTIMATED**: arithmetic on measured and derived inputs.
- **NOT VERIFIED**: would need a live read, which this analysis did not make.

No RPC, Blockscout or other provider call was made for this document. The
running discovery shadow (`SHADOW_DISCOVERY_B`) was only read, through a
read-only SQLite connection.

## 1. Summary

1. The census is correct and fail-closed. It is **not** the reason the five
   probed tokens stayed UNKNOWN in the way the probe summary suggested:
   - one census (microduck) was cut off by the **probe's own** 70-call budget,
     not by the production budget of 240;
   - for the other four the probe skipped the census on purpose.
2. Census cost is driven by **token age** (DERIVED): the `Initialize` scan
   reads two log streams from the creation block, and each pool's
   `ModifyLiquidity` stream reads from that pool's creation. At 100 000-block
   windows and ~9.9 blocks/s (MEASURED), one window is ~2.8 h of history.
3. An early entry only accepts tokens up to 6 h old
   (`orchestration/strategy/early.py:84`). For those tokens a census is
   ~20–35 reads (ESTIMATED), far below the 240 cap. The census in its current
   form fits early entries; it fails for tokens older than ~8 days (ESTIMATED).
4. **Correction to the 2026-10-14 probe report:** RICH's 77.16 % top holder is
   `0x000000000000000000000000000000000000dead`, a burn address (MEASURED from
   the stored snapshot, `is_burn_address=true`). It is not a "non-contract
   account holding 77 %". Without the burn row, RICH's top ten is about 15 %.
5. The proposed direct-holder floor is mathematically a safe lower bound of the
   economic figure. On the five stored snapshots it would have blocked
   **nothing** in its strict form (section 7). Its value is avoiding the cost
   of a census for clear cases, not unlocking tokens.
6. Recommendation: **Option C**, in stages. First, cheap early refusals and a
   zero-read age check before the census. Then an incremental, cached census
   for re-evaluations. The full census stays mandatory before any positive
   release. Option D is the fallback for every V4-path venue whose custody is
   not supported.

## 2. Inputs

| Input | Where |
|---|---|
| Census, decoders, economic view, custody | `backend/src/agents/atlas/v4/` |
| Snapshot wiring | `backend/src/agents/atlas/context.py:237-290` |
| ATLAS policy (no own concentration limit) | `backend/src/agents/atlas/policy.py:136` |
| SENTINEL limit 0.35, strict `>` | `backend/src/core/models.py:193`, `backend/src/risk/engine.py:126-132` |
| Entry figure: raw, economic or floor | `backend/src/orchestration/riskrequest/service.py:746-772,886-894` |
| Stored probe evidence (5 tokens) | session scratchpad `snapshot_*.json`, `result_*.json` (not in Git) |
| Discovery shadow, interim, read-only | `~/.rh-agents-shadow/discovery-b-20261014/shadow.sqlite`, run `20261014T184926Z` |
| Offline tests run | `tests/atlas/v4`, `tests/runner/test_pool_control_wiring.py`: 191 passed (in-memory SQLite, no network) |

## 3. Pool architectures

### 3.1 How the code decides the version

There is no mapping from dex id to protocol (DERIVED). The only split is the
length of the pool address GeckoTerminal returns:

- A 32-byte id becomes `BYTES32_POOL_ID`, which is treated as Uniswap V4
  (`markets/geckoterminal/adapter.py:113-116`, `markets/models.py:79-109`).
- A 20-byte address becomes `CONTRACT_ADDRESS`, an ordinary market.

Pool control is *required* when the market is `BYTES32_POOL_ID` or when the
census found V4 pools for the token (`context.py:284`).

The venues seen on Robinhood Chain by the running discovery shadow (interim:
114 polls, ~9.5 h; MEASURED, provisional, not a shadow result):

| GeckoTerminal dex id | Id length | Distinct pools | Code path |
|---|---|---|---|
| `uniswap-v4-robinhood` | 32 bytes | 813 | V4 census |
| `pons-v2` | 20 bytes | 448 | raw holders |
| `uniswap-v2-robinhood` | 20 bytes | 79 | raw holders |
| `uniswap-v3-robinhood` | 20 bytes | 71 | raw holders |
| `bankr-robinhood` | 32 bytes | 28 | V4 census |
| `uniswap-pools-trade` | 32 bytes | 10 | V4 census |
| `pons-v2-dex` | 32 bytes | 5 | V4 census |
| `clanker-robinhood` | 32 bytes | 2 | V4 census |
| `pancakeswap-infinity-clmm-robinhood` | 32 bytes | 2 | V4 census (see below) |
| `ramses-v3-robinhood` | 20 bytes | 2 | raw holders |
| `virtuals-robinhood` | 20 bytes | 1 | raw holders |

A 32-byte id says nothing about *which* PoolManager holds the pool:

- **bankr:** microduck's stored holder rows show the configured PoolManager
  `0x8366…0951` holding 99.97 % of supply (MEASURED). So at least this bankr
  pool lives in the Uniswap V4 PoolManager.
- **pancakeswap-infinity:** Pancake Infinity has its own vault and pool
  manager (public design; on this chain NOT VERIFIED). The census, which knows only `0x8366…0951`, cannot find such a pool
  and ends `V4_MARKET_POOL_NOT_FOUND`. That is UNKNOWN, fail closed (DERIVED).
- **clanker, pools-trade, pons-v2-dex:** which PoolManager they use is
  NOT VERIFIED.

### 3.2 Capability matrix

SUP = supported, PART = partial, NO = not supported.

| Capability | Uniswap V4 (`0x8366…`) | V2 pair | V3 pool | Launchpad on V4 (bankr, clanker, …) | Launchpad / V4 fork on another manager |
|---|---|---|---|---|---|
| Discovery | SUP: `new_pools`, no venue filter | SUP | SUP | SUP | SUP |
| Market data | SUP: GeckoTerminal | SUP | SUP | SUP | SUP |
| Liquidity | PART: GeckoTerminal `reserve_in_usd` only, no on-chain read | PART | PART | PART | PART |
| Holder data | SUP: Blockscout raw rows; the PoolManager row is replaced by position controllers | PART: raw only, the pair counts as a holder | PART: raw only, the pool counts as a holder | as V4 | raw rows; census cannot attribute |
| Pool control | SUP: census (`census.py`) | **NO** | **NO** | SUP if the pool is in `0x8366…` | **NO**: `V4_MARKET_POOL_NOT_FOUND` |
| Position control | PART: account owner, or Uniswap Liquidity Launcher v3.3.0 FeeSplitter / TimelockedPositionRecipient only | **NO** | **NO** | PART: launchpad lockers and hooks become `UNKNOWN_CONTRACT_CUSTODY` / `OWNER_UNKNOWN` | **NO** |
| Quote | SUP: KyberSwap route, not bound to the case's pool | SUP | SUP | NOT VERIFIED (KyberSwap coverage) | NOT VERIFIED |
| ANCHOR | no version-specific checks | same | same | same | same |
| SENTINEL (buy) | economic top ten, else UNKNOWN | raw top ten vs 0.35 | raw top ten vs 0.35 | economic, else UNKNOWN | UNKNOWN |
| Early-entry eligibility | PART: needs `ATLAS_V4_POOL_CONTROL_ENABLED=true`, a complete census and economic top ten ≤ 0.35 | PART: no LP-control check exists, so the pair's reserve counts against 0.35 | PART: as V2 | effectively blocked unless custody is supported | blocked (UNKNOWN) |

### 3.3 Gaps

1. **V2 and V3 have no pool-control layer at all.** The market is neither
   UNKNOWN nor blocked; the pair or pool contract is one raw holder. This is
   not fail-closed for LP control. Invariant 8 asks unclear LP control to
   prevent a BUY, but the code never establishes LP control for 20-byte
   markets, so it never notices that it is unclear. This is the larger safety
   gap: pons-v2 alone is a third of the shadow's pools. It is outside this
   order's scope; it is listed for the architecture review.
2. **Launchpad custody on V4.** Only the official Uniswap launcher templates
   are verified. A bankr or clanker locker, hook-held liquidity, or any proxy
   ends as unresolved custody or unknown owner. With more than 1 % of supply
   behind it, the economic figure is UNKNOWN (`economic.py:197-228`). Which
   custody bankr actually uses is NOT VERIFIED.
3. **V4 forks on another manager** (Pancake Infinity) take the V4 path
   because of their id length and always end UNKNOWN. That is safe, but they
   are not supported.

## 4. The V4 census algorithm (DERIVED)

All reads are pinned to the snapshot block, sequential, and charged to one
budget (`CensusBounds`: 100 000-block windows, 240 requests, 16 pools,
64 positions, 4 000 position events, 30 s).

| Step | Reads | Code |
|---|---|---|
| 1. Chain id | 1 | `census.py:284-286` |
| 2. Deployment: PoolManager code; per PositionManager its code and `poolManager()` | 1 + 2 × 1 = 3 | `census.py:322-348` |
| 3. Creation block from Blockscout `getcontractcreation`, proven: no code at `created-1`, code at `created` | 2 | `census.py:350-362` |
| 4. Pre-check: `requests + 2 × windows > 240` refuses before any log read | 0 | `census.py:294-298` |
| 5. `Initialize` on the PoolManager, token as `currency0` (topic 2) and as `currency1` (topic 3): two streams over `[created, block]` | 2 × W | `census.py:401-453` |
| 6. Per pool: creation-block timestamp (cached per block), Slot0 via `extsload`; with a hook, hook code and `owner()` | 2–4 per pool | `census.py:455-523` |
| 7. Per pool: `ModifyLiquidity` (topic 1 = pool id) over `[pool.created_block, block]`, net liquidity per `(pool, sender, ticks, salt)` | W_p per pool | `census.py:525-578` |
| 8. Per active position: stored liquidity via `extsload`, must equal the reconstruction | 1 per position | `census.py:586-602` |
| 9a. PositionManager position: `getPoolAndPositionInfo` (bound to pool and ticks), `ownerOf`, owner code once per owner, custody adapter | 3 + adapter per position | `census.py:631-738`, `custody/resolver.py` |
| 9b. Direct position: sender code; an account is the owner, a contract is `OWNER_UNKNOWN` | 1 per position | `census.py:740-778` |
| 10. `balanceOf(token, PoolManager)` | 1 | `census.py:306-307` |

W = ⌈(block − created + 1) / 100 000⌉. Adaptive narrowing halves a window on
`RPC_ERROR`, `CONTRACT` (which includes a possibly cut 5 000-log page) or
`TIMEOUT`, keeps the narrower span for the rest of the stream, and stops at one
block. Every attempt is a budgeted read.

The economic view (`economic.py`):

- Removes the PoolManager row from the raw rows.
- Adds each attributed position's exact token amount to its controller.
- Puts permanently locked supply in no holder's row.
- Ranks the PoolManager balance not traced to a position as the pseudo-holder
  `UNATTRIBUTED_POOL_BALANCE`.

It refuses (UNKNOWN):

- when positions exceed the PoolManager balance;
- when the unattributed rest is above 1 % of supply;
- when the market pool is missing;
- when custody is unresolved. That case keeps only `economic_top10_floor`.

**Completeness safety net.** The PoolManager's `balanceOf` is a global
reconciliation. Supply in any pool the scan missed shows up as unattributed and
fails the 1 % bound. That missed pool can be one initialized before the token
existed, which `Initialize` from the creation block cannot see, or one in a
skipped range. This is what makes several optimizations below safe.

### Topic and protocol facts

- `Initialize(bytes32 id, address currency0, address currency1, uint24, int24, address hooks, uint160, int24)`:
  - The id is checked against the keccak of the decoded key.
  - A second `Initialize` for the same id is refused, because V4 initializes
    a pool once.
- `ModifyLiquidity(bytes32 id, address sender, int24, int24, int256, bytes32 salt)`:
  - `sender` is the PoolManager owner key, which is the PositionManager for
    NFT positions.
  - The salt is the NFT token id.
- Hook permissions come from the low 14 address bits, which is exact protocol
  semantics. No policy acts on hook facts yet.
- Liquidity removal is the negative `liquidityDelta`. Net negative history is
  refused as incomplete.

## 5. Why the five probe tokens stayed UNKNOWN

| Token | Venue | Age (blocks, MEASURED) | W | Probe outcome | Production census (ESTIMATED) |
|---|---|---|---|---|---|
| microduck | bankr | 1 003 081 | 11 | Census ran with `max_requests=60` inside a probe guard of 70 RPC per token. The guard raised `BudgetStop` mid-census, `context.py:272-278` mapped it to `V4_POOL_CENSUS_UNAVAILABLE` with `requests_made=0`. | ~50–60 reads: fits 240 |
| ANTIROBINHOOD | uniswap-v4 | 1 003 223 | 11 | Census not run (stage 2 without census, by budget decision) | ~50–60: fits |
| saluteface | uniswap-v4 | 1 864 594 | 19 | not run | ~75–85: fits |
| RICH | uniswap-v4 | 10 472 422 | 105 | not run | pre-check passes (6 + 210 ≤ 240); `ModifyLiquidity` alone needs ~105 more, so `V4_POSITION_FACTS_INCOMPLETE` (bound) |
| GME | bankr | 88 530 310 | 886 | not run; holders also `UNSUPPORTED_ENDPOINT` | refused by the pre-check after 6 reads (`CENSUS_BOUNDS_EXCEEDED`) |

So:

- **None of the five shows a census defect.** Three would have completed in
  production; two are too old for any budgeted full census.
- **All five were 28 h to 126 days old.** None was inside the 6 h early-entry
  window, so they are not representative of early entries.

The probe's `requests_made=0` for microduck is a probe artefact: a foreign
exception unwound the census before it could report its count.

## 6. RPC cost

### 6.1 Inputs

| Quantity | Value | Tag |
|---|---|---|
| Block rate | 9.905 blocks/s (ANTIROBINHOOD: 1 003 223 blocks in 101 287 s); 9.78 (RICH, 12.4 days) | MEASURED |
| History per 100 000-block window | ~2.8 h | ESTIMATED |
| RPC latency | 70 reads in 5.2 s, ~74 ms each, sequential | MEASURED (probe stage 1) |
| Live case of 2.66 M blocks | 94 reads, 100 000-block windows answered | MEASURED earlier (`docs/atlas-v4-pool-control.md`, Known limits) |
| Provider's real maximum log span and 5 000-log cap behaviour | | NOT VERIFIED |

### 6.2 Formula (DERIVED, assuming one pool created with the token)

```
C(W, k) ≈ 6 fixed + 2W Initialize + 4 pool (with hook) + W ModifyLiquidity
          + 3k positions + ~3 owner/custody + 1 balance
        ≈ 14 + 3W + 3k
```

Check: W = 27, k = 1 gives 98, against the measured 94.

| Token age | W | Reads (k = 2) | Tag |
|---|---|---|---|
| ≤ 2.8 h | 1 | ~23 | ESTIMATED |
| ≤ 6 h (early limit) | 2–3 | ~26–29 | ESTIMATED |
| 28 h | 11 | ~53 | ESTIMATED |
| 52 h | 19 | ~77 | ESTIMATED |
| ~8.5 days | 73 | ~239: the practical ceiling | ESTIMATED |
| ~13.7 days | 117 | pre-check ceiling; fails later in positions | DERIVED |
| > 13.7 days | > 117 | refused after 6 reads | DERIVED |

- **Additional costs:** each extra pool adds ~4 + W_p. Each extra position adds
  3 plus custody reads (a refused unknown contract reads two EIP-1967 slots).
- **Minimum:** a young token, one pool without a hook, one account-owned
  position: ~18.
- **Worst case:** the census is always capped at 240. It is reached by:
  - a provider that answers only narrow windows (25 000 blocks quadruples W);
  - 16 pools;
  - up to 64 positions.
- **Wall clock:** 240 reads at the measured 74 ms is ~18 s, inside the 30 s
  bound (ESTIMATED). A slower provider turns into `TIMEOUT`, which is UNKNOWN.

### 6.3 Behaviour at provider limits (DERIVED)

- **Splittable failures** (`RPC_ERROR`, `CONTRACT`, `TIMEOUT`): the window is
  halved and the narrower span is kept for the rest of the stream.
- **Rate limit, credentials, chain identity or an unavailable provider:** the
  census fails immediately and is never split.
- **Chain-id mismatch:** a hard refusal.
- **A cut 5 000-log page:** treated as `CONTRACT` and narrowed, never read as
  complete.
- **Budget exhausted:** `V4_POOL_CENSUS_BOUNDS_EXCEEDED` or
  `V4_POSITION_FACTS_INCOMPLETE`. Never a short list reported as complete.

### 6.4 Repetition

The census is stateless: every ATLAS snapshot repeats it from the creation
block. How often the same token is re-evaluated inside its 6 h window is
NOT VERIFIED. At a 15-minute cadence it could be up to 24 times, which would
be ~24 × 25–30 reads per token (ESTIMATED).

## 7. The direct-holder concentration floor

### 7.1 Definition

The floor F is the sum of the ten largest **raw** holder rows that remain
after excluding:

- the configured PoolManager (`0x8366…0951`);
- the burn addresses `0x0` and `0x…dead`;
- every row that is not proven an account, meaning `eth_getCode` at the pinned
  block is `0x` or an EIP-7702 delegation;

all over the on-chain total supply.

Rule: if F > 0.35, then BLOCK (`HOLDER_CONCENTRATION_LIMIT`). Otherwise the
gate stays exactly as it is, which is UNKNOWN without a census. F never
produces PASS.

### 7.2 Why it is a lower bound

Let E be the economic top-ten the census would compute (`economic._ranked`).

1. For every retained address other than the PoolManager, E's balance is the
   raw balance plus any controlled position amount. That is never less than
   the raw balance.
2. E also adds rows the raw view lacks: new controllers and the unattributed
   pseudo-holder.
3. The sum of the ten largest values of a multiset never decreases when
   values grow or rows are added.
4. F sums only a subset of E's rows, each at a value no greater than in E.
5. Therefore F ≤ E, for both the exact and the upper-bound basis.

Excluding rows only lowers F. So F also stays below E under a future policy
that drops burn or contract rows.

### 7.3 Requirements

| Question | Answer |
|---|---|
| PoolManager share to subtract | The PoolManager's whole row: every unit of it is re-assigned by the census or unattributed. |
| Custody contracts | Excluded through the account-only rule: lockers, FeeSplitter, TimelockedPositionRecipient, vesting, pairs and V3 pools. |
| PositionManager | Excluded: it holds no position principal, and tokens left in it can be swept by anyone. The same applies to routers. |
| Holder quality | `COMPLETE` or `TOP_N_ONLY` (a true prefix is enough, because missing rows only lower F); the observation basis the policy accepts; `holders.total_supply_raw` must equal the on-chain supply; skew inside `max_source_skew`. Unresolved provider exclusions are acceptable: a filtered row is missing from F and can only lower it. |
| Double counting | None. F adds no position amounts, and raw balances are disjoint units. Indirect ownership (an account and a contract it owns) is counted separately or not at all, so it understates. |
| Account check | One `eth_getCode` per candidate row, at most 10, or zero when no row would bring F above 0.35. Blockscout's `is_contract` is a claim and does not replace it. |

### 7.4 Can a legitimate token be blocked wrongly?

- **By the strict floor:** only in three ways, and each is a policy matter,
  not a floor error:
  - an unlisted burn sink without code (e.g. `0x…0001`) holding more than
    35 %;
  - a stale holder row within the accepted skew;
  - a custodial account, such as an exchange hot wallet, counted as one
    holder.
- **By a floor that keeps burn rows: yes.** The current SENTINEL metric is top
  ten over total supply **including** burn rows
  (`workflow/models.py:291-296`, `normalize.py:115-118`), and the economic view
  keeps them as well. RICH shows the effect:

| Token | Raw top 10 | Without PoolManager | Without PoolManager and burn | Accounts only (provider flag) | Strict floor blocks? |
|---|---|---|---|---|---|
| microduck | 1.0000 | 0.0003 | 0.0003 | 0.0000 | no |
| ANTIROBINHOOD | 1.0000 | 0.0000 | 0.0000 | 0.0000 | no |
| saluteface | 0.6367 | 0.1968 | 0.1968 | 0.0676 | no |
| RICH | 0.9193 | 0.9193 (burn 0.7716) | 0.1520 | 0.1520 | no |
| GME | n/a: no holder data | | | | n/a |

Offline replay over the stored snapshots (MEASURED inputs, DERIVED figures).

- A floor that only removes the PoolManager would block RICH, purely because
  of its burn. A full census would do the same today: the economic view keeps
  the burn row. Whether burned supply should count toward the 0.35 limit is a
  policy question for the architecture review. This order changes no policy.
- The strict floor would have blocked none of the five tokens. It saves census
  reads only for tokens with a real account concentration above 35 %.

## 8. Optimization options

The false-PASS column means added risk over today's design.

| | Option | Safety effect | Data | RPC | Effort | Failure modes | Young tokens | False PASS |
|---|---|---|---|---|---|---|---|---|
| A | Smaller ranges for young pools | none: the range already equals the age | none | already minimal (W = 1–3) | none | n/a | already optimal | none |
| B | Known pool initialization as start: verify the market pool's `Initialize` by pool-id topic in one window around GeckoTerminal's `pool_created_at`, and skip the full two-stream scan | relies on the balance reconciliation for missed pools: more UNKNOWN, no false PASS | discovery pool id and time | 2W reads become 1–2 | M | wrong claimed time costs one more read, then falls back to the full scan; more than 1 % in other pools gives UNKNOWN | small gain (W ≤ 3) | none beyond today's 1 % tolerance |
| C | Event-driven instead of repeated full scans | same as E | | | | | | |
| D | Reuse immutable evidence: deployment code and `poolManager()` binding, proven creation block, pool keys, `Initialize` events, creation timestamps, custody code hash → verdict | none: all immutable, keyed by chain id, addresses, block, code hash | local cache | saves 6 + 2W + 1 per pool per repeat | S–M | cache poisoning by a bug: key on chain id and verify on read | high | none, if keys are exact |
| E | Incremental census: keep `(pools, net liquidity per position key)` up to block B, scan only `(B, now]`. Still re-read per run: each position's `extsload`, `ownerOf` (NFTs move), PoolManager balance, Slot0 | two independent checks stay: storage equality per position, balance reconciliation | cache with the canonical hash of B | repeat ≈ 1 hash check + 2 + P + 3k + 1 ≈ 12–20, instead of 14 + 3W + 3k | M–L | reorg past B: check B's hash, drop on mismatch; a missed event shows as a storage mismatch or unattributed rest, so UNKNOWN | high; makes week-old tokens censusable | none: a stale cache fails closed through both checks |
| F | Cache invalidation | rule: invalidate on hash mismatch, chain id or deployment change, any storage mismatch; never extend validity by time | | | S | | | none |
| G | Early refusal by the strict floor (section 7) | BLOCK only | holder rows, ≤ 10 `eth_getCode` | saves the whole census when it fires | S | stale rows: policy skew applies | yes | none (never PASS) |
| H | Early recognition of unsupported custody: stop once a pool's hook or launchpad custody is a known-unsupported pattern, or the market id is not in the configured PoolManager | UNKNOWN sooner; never PASS | hook address bits, known-pattern list | saves the position phase | M | a pattern list that is too broad gives UNKNOWN, not PASS | yes | none |
| I | Verifiable indexer data: Blockscout logs for `Initialize`/`ModifyLiquidity` in one request, every log re-verified on chain (pool-id hash, `extsload` equality, balance reconciliation) | completeness rests on reconciliation, as in B | existing Blockscout source; no new provider | W windows become a few pages | M | indexer lag means missing logs, which gives UNKNOWN; rate limits | small gain | none beyond the 1 % tolerance |
| J | Query tuning: zero-read age pre-check from the origin block before the 6 fixed reads; measure and, if the provider allows, a larger span (not now); merge the two `Initialize` orientations only if the provider accepts OR filters (the topics of one log cannot be ORed across positions) | none | | J1 saves 6 reads per hopeless token | S | | none | none |

Not recommended: any estimate in place of a missing position, owner or custody
fact. That would violate section 8 of the order and invariant 8.

## 9. Architecture options

| | Option | Safety | Provider load | Early coverage | Verdict |
|---|---|---|---|---|---|
| A | Census unchanged | fail-closed, proven | ~25–30 reads per young evaluation, repeated each snapshot; old tokens fail | young V4 tokens in `0x8366…` with supported custody only | acceptable baseline |
| B | Technical optimization only (D, E, F, J) | unchanged | repeats drop to ~12–20 | same set; older tokens become censusable | good, but no early refusals |
| C | Staged: zero-read checks → strict floor (BLOCK only) → custody and manager recognition (UNKNOWN only) → full or incremental census before any positive release | unchanged for PASS: only refusals become cheaper | lowest | same as A or B | **recommended** |
| D | No V4 early entries for now, keep observing | maximal | zero census | none on V4 (32-byte venues are ~59 % of shadow pools, interim) | fallback per venue, not for `uniswap-v4-robinhood` |

**Recommended architecture:** C, staged, with D applied per venue.

Order of checks for a V4-path early candidate. Each stage can only refuse; the
last stage can release:

1. **Zero-read gates (J1, H part 1):**
   - The market id is in no configured PoolManager → UNKNOWN.
   - The age from the origin block exceeds what the remaining budget can scan
     → UNKNOWN.
2. **Strict floor (G):** F > 0.35 → BLOCK.
3. **Census:** full (A) today; with cache and incremental scanning (D, E, F)
   once built.
4. **Custody stage (H):** unresolved custody → UNKNOWN with the existing floor.
5. **Release:** only an `AVAILABLE` economic figure ≤ 0.35 passes, exactly as
   today.

Venues whose custody no adapter verifies stay effectively Option D, as they
are already. That means Pancake Infinity, and bankr or clanker until their
custody is verified. No code change is needed for that, because they fail
closed.

**Expected RPC reduction (ESTIMATED):**

- A first evaluation of a young token is unchanged (~25 reads).
- Re-evaluations drop ~40–60 % with E plus D (~12–20 reads instead of ~25–35).
  For tokens of 1–8 days the drop is 70–90 %.
- Tokens with a clear account concentration cost ≤ 10 reads instead of a
  census.
- Hopeless tokens cost 0 instead of 6.
- The total depends on how often a token is re-evaluated, which is
  NOT VERIFIED.

## 10. Safety implications

- No rule changes:
  - top ten at most 0.35;
  - incomplete evidence is UNKNOWN;
  - UNKNOWN allows no BUY;
  - an unsupported structure or unclear LP control allows no BUY;
  - no estimates replace evidence.
- Every new stage can only refuse. PASS still requires an `AVAILABLE` census
  and economic figure.
- Caching adds one risk: stale evidence. It is contained by:
  - block-hash checks;
  - immutable-only reuse;
  - the two chain-side integrity checks that run on every evaluation.
- Open policy questions for the review, not decided here:
  - Should burned supply count toward the limit? Today it does (RICH).
  - V2/V3 have no LP-control layer (section 3.3).

## 11. Proposed implementation (specification only)

Each stage is a separate PR with its own regressions. None is part of this
order.

1. **Stage 1: floor and zero-read gates.**
   - Add `direct_holder_top10_floor` to `PoolControlFacts` and its workflow
     record. It is allowed with any census gap, not only
     `POSITION_CONTROL_UNRESOLVED`. The two model validators that today
     reserve a floor for that gap
     (`v4/models.py:288`, `workflow/models.py:561`) need a separate field
     rather than a loosened rule.
   - `entry_concentration` uses `max(economic floor, direct floor)` only while
     no economic figure exists, and `entry_concentration_established` stays
     false.
   - Account proof by `eth_getCode` at the pinned block, charged to a small
     budget.
   - Move the age pre-check before the fixed reads.
2. **Stage 2: unsupported-manager and custody recognition.** Recognize a market
   pool id that is not in the configured PoolManager (a single `extsload`
   Slot0 read: zero means not initialized there) → `V4_MARKET_POOL_NOT_FOUND`
   before any scan.
3. **Stage 3: immutable-evidence cache and incremental census (D, E, F).**
   - Cache keyed by chain id, PoolManager, token and block hash.
   - Storage equality and balance reconciliation unchanged.
4. **Separately (review decision):** V2/V3 LP-control layer; burn semantics.

## 12. Required regressions

- **Floor:**
  - It never yields PASS.
  - F > 0.35 with any census gap → `HOLDER_CONCENTRATION_LIMIT`; F ≤ 0.35 →
    still `HOLDERS_UNKNOWN`.
  - Burn and PoolManager rows are excluded.
  - A contract row is excluded even when the provider says `is_contract=false`.
  - An EIP-7702 account is included.
  - A `TOP_N_ONLY` prefix and unresolved exclusions only lower F.
  - Denominator mismatch, an unaccepted observation basis or excess skew mean
    no floor.
  - Property test: F ≤ E over random holder/position sets.
- **Zero-read pre-check:** a too-old token makes no read at all, and its
  outcome stays `CENSUS_BOUNDS_EXCEEDED`.
- **Market pool not in the configured manager:** UNKNOWN, no log read.
- **Cache:**
  - A reorged block hash drops the cache.
  - A missed `ModifyLiquidity` gives a storage mismatch, so UNKNOWN.
  - A pool missed before B gives an unattributed rest above 1 %, so UNKNOWN.
  - `ownerOf` is re-read on every run, so a transferred NFT changes the
    controller.
  - Cache keys never cross chains.
- **Existing suites unchanged:** `tests/atlas/v4` (191 passed offline at
  `9234fbf`, including the wiring test), `test_revenue_regression.py`, and an
  exit read that never waits on a census.

## 13. Not verified

- Which PoolManager clanker, pools-trade and pons-v2-dex use.
- bankr's custody contract.
- Whether KyberSwap routes these venues.
- The RPC provider's maximum log span and its log cap.
- How often one token is re-evaluated inside its 6 h window.
- The final discovery-shadow figures (the run ends 2026-10-15 ~20:49 CEST).
