# Early PAPER readiness: what has to hold, and the shortest safe path

State after the LP-control integration (PRs #95, #99, #100, #105, #107, #108)
and the discovery work (#101, #109). Nothing here changes a risk limit; every
step that touches the runtime, a provider or a scheduler needs its own
approval.

## What an early PAPER BUY has to pass

| Gate | Where | What passes |
|---|---|---|
| Discovery | scout (`EARLY_SCOUT_*`) | a young pool, observed; with the V4 prefilter only 32-byte V4 ids go on to ORBIT and the early intake |
| ATLAS V4 census | `agents/atlas/v4/census.py` | complete census bound to the verified PoolManager, chain and ATLAS block; dated by its block time (≤ 30 s at SENTINEL) |
| Pool discovery | pool index (#94) | `MANAGER_HISTORY`: the index covers the PoolManager from its deployment to the token's creation, and the chain still has its tip |
| Reconciliation and credit | #96, #103 | every unit of the PoolManager remainder credited exactly: fees to a withdrawable position's controller, protocol fees, named claim holders, surplus to nobody; FeeSplitter token fees are **not** creditable yet |
| Position control | #97 | every position in the market pool `PERMANENTLY_LOCKED`; creator-withdrawable → BLOCKED; anything else withdrawable or unresolved → UNKNOWN |
| Hook | `lp_control_view` | the market pool has no hook (a creator-owned hook → BLOCKED, any other → UNKNOWN) |
| Binding | #98/#105 | market pool pairs the market's token; a named PoolManager is the verified one; 20-byte pools are `UNSUPPORTED` |
| Holders, liquidity, slippage, freshness | SENTINEL | unchanged limits (35 % top ten, early minimum liquidity 10 000 USD, configured slippage, 30 s) |
| Executor | `execution/paper.py` | the approved, fingerprint-bound market; refuses any BUY without established LP control |
| Exit continuity | runner | `PAPER_ENTRY_REQUIRES_EXIT_JOB`: every BUY needs a recent ready exit-job run (`EXIT_JOB_STALE` otherwise) |

The offline acceptance (`tests/acceptance/test_early_paper_offline.py`) runs
this chain with the production components and shows each unsafe LP-control
case stopped at SENTINEL and again at the executor's own LP-control check
(with an approval forged to pass every earlier executor check), while a sale
out of the same market, read with ATLAS's exit policy, still fills. A stale
census is stopped by SENTINEL; the executor relies on the approval's expiry.

## Which token can pass today, realistically

An official InstantLaunch-style V4 pool whose launch NFT sits in a registered
FeeSplitter, with no other position in its pool, no hook, a receipt-verified
creator, a holder distribution under the limit — **and no token-side swap fee
owed to the locked position yet**. Token-side fees accrue on every sell into
the pool and cannot be credited until the FeeSplitter's token-fee recipient is
verified (`docs/fee-splitter-fee-credit.md`). They cannot be: the compounder
lets anyone claim them (`docs/fee-splitter-verification.md`). With a discovery
delay of about four minutes (shadow run B, p50) most pools will have seen a
sell before ATLAS reads them, so in practice only launches read before their
first sell can pass, unless a worst-case treatment of claimable fees is decided.

## Shortest safe path to a releasable early PAPER candidate

1. **Code** (no approval beyond review): merge the open acceptance and
   backfill-readiness PRs.
2. **FeeSplitter evidence**: done offline — token fees are permissionlessly
   claimable, never creditable; a 13-read live confirmation remains. A
   decision on a worst-case treatment of claimable fees (an exact upper bound,
   not a tolerance) is the only way traded launches could pass.
3. **Runtime deploy and migrations** (approval): deploy main, then
   `alembic upgrade head` for 0025 (scout discovery gaps) and 0026 (pool index)
   on the runtime database; the preflight refuses the new code until then.
4. **Pool index backfill** (approval with RPC budget): `index_job` in calls of
   800 reads until complete; ≈ 3 100 reads at 100 000-block windows,
   ≈ 12 400 if the provider caps at 25 000 (`docs/atlas-v4-pool-index.md`).
   Cross-check against a second RPC endpoint.
5. **Settings** (approval): `ATLAS_V4_POOL_CONTROL_ENABLED=true`,
   `ATLAS_V4_POOL_INDEX_ENABLED=true`, the discovery profile below, the early
   entry flags (`PRE_VECTOR_EARLY_ENTRY_ENABLED`, `EARLY_PAPER_EXIT_ENABLED`,
   `PAPER_EARLY_ONLY`), `PAPER_ENTRY_REQUIRES_EXIT_JOB=true`.
6. **Schedulers** (approval): the index job every 5 minutes (≈ 7 reads per
   run), the scout at 300 s, and a running exit job before any entry job.
7. **Controlled PAPER acceptance**: one bounded entry run, observed, with the
   exit job live.

## Discovery profile (offline-verified: `tests/scout/test_target_profile.py`)

| Setting | Value |
|---|---|
| `RH_AGENTS_SCOUT_INTERVAL_SECONDS` (ops/scout/install.sh) | `300` |
| `EARLY_SCOUT_MAX_DISCOVERY_POOLS` | `20` |
| `EARLY_SCOUT_MAX_NEW_WATCHES_PER_RUN` | `40` |
| `EARLY_SCOUT_DISCOVERY_PAGE2_ENABLED` | `true` |
| `EARLY_SCOUT_MAX_DISCOVERY_PAGE2_PER_DAY` | `72` |
| `EARLY_SCOUT_V4_PREFILTER_ENABLED` | `true` |
| `EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_RUN` / `_PER_DAY` | `≤ 10` / `96` (model calls; budget decision) |

Provider volume: ≈ 288 GeckoTerminal requests a day for page 1, ≈ 345 with
saturation-driven page 2 (cap 72 a day). Reinstalling the scout must respect
the scout-unload gate (`ops/scout/install.sh` still has an unconditional
`bootout`, issue #77).

## Required approvals

1. Merging the open exit-isolation PR (#115); acceptance, backfill readiness and the V2/V3 architecture are merged.
2. The FeeSplitter live reads (13 reads, `docs/fee-splitter-verification.md`); the public-source work is done.
3. Runtime deploy plus migrations 0025 and 0026.
4. RPC budget for the index backfill (≈ 4 000 reads, ≈ 13 000 worst case) and
   for its 5-minute incremental runs (≈ 2 000 reads a day).
5. Settings and schedulers in steps 5 and 6, and the ORBIT model budget.
6. The controlled PAPER acceptance run itself.
