# Phase 2D: ATLAS, the first safety-critical specialist

ORBIT performs discovery. ATLAS produces on-chain safety evidence that can stop a
TradeCase, which makes it materially more sensitive: a wrong CLEAR here is a
safety failure, not a missed opportunity.

The whole design follows from one rule: **LLM reasoning is never the authority
that decides whether required safety facts exist, or whether a deterministic
blocker may be ignored.**

```
chain / indexer sources
        |
        v  narrow typed ports, each returning availability + provenance
AtlasSnapshotBuilder            (deterministic, no model)
        |
        v  AtlasOnchainSnapshot
AtlasPolicy  (versioned, code-defined)
        |
        v  AtlasSafetyDecision: CLEAR / BLOCKED / INSUFFICIENT_DATA
        |
        +-- optional ReasoningProvider -> advisory commentary only
        |
        v
ONCHAIN_EVIDENCE  ->  deterministic TradeCase evaluator
```

The verdict is computed **before** any model runs. Nothing after that step can
change it.

## Three concepts that must never merge

| Concept | Values | Who decides |
| --- | --- | --- |
| Fact availability | AVAILABLE / UNKNOWN / UNAVAILABLE | the source |
| Safety verdict | CLEAR / BLOCKED / INSUFFICIENT_DATA | deterministic policy |
| Interpretation | advisory findings and a summary | the model, with no authority |

There is no `safe = true` and no confidence score anywhere in the chain.

### Known bad is not unknown

This distinction drove a change to Phase 2A.

A measured violation — say the token address holds no contract code — is an
**available** fact whose content blocks. An unreachable holder provider is an
**unavailable** fact that also blocks, for a different reason and with a
different remedy. Collapsing the first into the second would hide a measured
danger behind a missing measurement and destroy the audit trail.

## The Phase 2A extension this required

**Audit finding.** Before Phase 2D the evaluator asked only
`effective_status != AVAILABLE`. There was no path on which available evidence
blocked because of its *content*. Worse, `EvidenceSubmission` actively rejected
available on-chain evidence unless every integrity domain was `PASS`, so a known-
bad token could not be submitted as available at all — it would have had to
masquerade as `UNKNOWN` or `INVALID`.

**The extension** is one generic mechanism, not ATLAS-specific branching:

* `EvidenceAcceptance` (`ACCEPTED` / `BLOCKED` / `INSUFFICIENT`) is a second axis
  alongside `EvidenceStatus`, derived deterministically from the typed payload.
* Every payload answers `acceptance()`; most return `ACCEPTED` because their
  availability is the whole question. `OnchainPayload` derives it from its three
  domain verdicts: any `FAIL` blocks, any `UNKNOWN` is insufficient.
* `unusable_reason(envelope, now)` in the evaluator asks both questions in one
  place, and every requirement check goes through it.
* The submission validator now permits `AVAILABLE` + `FAIL` (a known violation is
  a fact, and must carry a reason code) while still refusing `AVAILABLE` +
  `UNKNOWN` (an unestablished domain does not belong in available evidence).

No worker, model, FUSE or COMMANDER can set or override acceptance. It is a pure
function of the stored payload.

## Fact model

`AtlasOnchainSnapshot` records what was established, per domain, with provenance:

* **ChainSnapshot** — chain, network, chain ID, block number and hash, observation
  time, source. All contract reads target that one block, so facts used together
  are consistent rather than assembled across a moving chain.
* **ContractFacts** — code presence, decimals, raw total supply, EIP-1967 proxy
  observation, implementation and admin addresses.
* **HolderFacts** — holder count, top holders, and concentration ratios, both raw
  and with proven burn addresses excluded, kept as separate numbers so a large
  position can never be hidden by an adjustment.
* **OriginFacts** — creator address, creation block and transaction.

Every group carries `status` and, when unavailable, a typed `AtlasSourceFailure`.

The snapshot digest covers the normalized facts and their provenance — including
block number and block timestamp — but not the moment of collection, so an
unchanged chain state hashes identically across passes. That is safe precisely
because freshness is enforced separately from observation time and cannot be
reset by re-collecting.
Model validators enforce the pairing: available facts cannot carry a failure, and
unavailable facts cannot carry observations. Supply is kept as an exact integer
alongside decimals; no value passes through a float.

### What is deliberately not claimed

* Non-standard methods such as `owner()` are never called. ERC-20 deployments are
  heterogeneous, and a call that reverts or succeeds proves nothing about intent.
* Empty EIP-1967 slots are recorded as `EIP1967_SLOTS_EMPTY`, which means those
  documented slots were read and were empty — **not** that the contract is not a
  proxy of some other pattern. `NOT_CHECKED` is a third, distinct state.
* A creator is never inferred from the first holder, the current owner or the
  pool creator.

## Provider capability matrix

> **Superseded by [Phase 2E](phase-2e.md).** The matrix below is what Phase 2D
> shipped, and it was accurate then: no holder or creator provider had been
> verified, so ATLAS deliberately shipped unable to reach CLEAR. Phase 2E adds
> verified sources for both domains. See [phase-2e.md](phase-2e.md) for the
> current support matrix. Nothing in this section was wrong; it was incomplete on
> purpose, and fail-closed while it was.

Filled only from what was implemented and verified at the time.

| Fact | Robinhood (4663) | BSC (56) | Source |
| --- | --- | --- | --- |
| Contract code presence | AVAILABLE | AVAILABLE | managed EVM RPC `eth_getCode` |
| ERC-20 `decimals()` | AVAILABLE | AVAILABLE | managed EVM RPC `eth_call` |
| ERC-20 `totalSupply()` | AVAILABLE | AVAILABLE | managed EVM RPC `eth_call` |
| EIP-1967 proxy / admin slots | AVAILABLE | AVAILABLE | managed EVM RPC `eth_getStorageAt` |
| Holder concentration | **UNAVAILABLE** *(2E: AVAILABLE)* | **UNAVAILABLE** *(2E: AVAILABLE)* | no verified indexer connected |
| Contract creator / deployer | **UNAVAILABLE** *(2E: AVAILABLE)* | **UNAVAILABLE** *(2E: AVAILABLE)* | needs creation history or archive access |
| Developer-wallet relationships | **UNAVAILABLE** | **UNAVAILABLE** | depends on creator provenance |

**Operational consequence as Phase 2D shipped it:** holder intelligence is a
required domain, and no verified provider existed for either chain. ATLAS
therefore could not reach CLEAR in a real deployment; it returned
INSUFFICIENT_DATA and the case blocked. That was the correct fail-closed
behaviour. Weakening the policy to let the pipeline proceed would have been
trading on evidence the system never had.

Plain EVM RPC cannot enumerate holders, and rebuilding a holder set from a
bounded log scan would produce a number that looks authoritative and is not. No
holder adapter was written against an unverified endpoint, and no provider was
assumed to support Robinhood Chain because it supports EVM chains generally.
Phase 2E follows exactly that rule: it connects providers whose responses were
verified against live API documentation and live responses, and leaves the
fail-closed path untouched for every chain and domain still unserved.

## Policy

The policy is code-defined and versioned. Thresholds live there, never in
prompt text and never chosen by a model. Phase 2D shipped `atlas-policy-v1`;
Phase 2E raises it to `atlas-policy-v2`, which additionally names the minimum
holder facts a source must establish before the holder domain counts as
satisfied.

* Required domains: CONTRACT and HOLDERS. ORIGIN is collected but not required,
  because no verified source exists and blocking on it would say nothing useful.
* Snapshot validity: 10 minutes — ATLAS's own policy, not borrowed from ORBIT's
  discovery window or from SENTINEL.
* Maximum source skew: 5 minutes. Facts from different sources are never atomic,
  so the spread is measured rather than assumed away. Phase 2E adds the caveat
  that the two operands are only epistemically equal when the holder fact is
  block-anchored; against a receipt-anchored one the same number bounds the
  pinned block's age at the moment of the answer and cannot see a lagging
  indexer, which is why assurance is gated separately.

### Freshness is anchored to observation, not to collection

Age is measured from `oldest_source_observation` — the earliest moment any
contributing source actually observed reality — and never from when the
collector ran. For contract facts that is the **block timestamp**: a block mined
twenty minutes ago is twenty minutes old however recently it was read. For
provider facts it is the best anchor that provider offers.

Phase 2E qualifies that last sentence, because "the best anchor offered" is not
always an observation time. A provider that names a block gives one
(`SOURCE_BLOCK`); a provider that names nothing leaves only the moment its
response was **received** (`RESPONSE_TIME`), which proves when a representation
arrived and not when the state behind it was observed. `HolderObservationBasis`
records which of the two a fact carries, `AtlasPolicy.accepted_holder_observation_bases`
decides which are good enough to act on, and `docs/phase-2e.md` carries the full
timestamp taxonomy and the pre-live requirement that follows from it.

This matters because the obvious implementation is wrong. If freshness were
anchored to collection time, a provider that keeps returning its 10:00 snapshot
would look fresh at 10:20 simply because the collector ran again. Re-fetching is
not evidence that the world moved. One current source also never rescues a stale
one: the oldest observation decides.
* Hard blockers: chain-ID mismatch, absent contract code, an available zero total
  supply, and — when configured — excess holder concentration or a present proxy
  admin.
* `max_top10_concentration` ships as `None`. The metric is measured and recorded,
  but the threshold blocker stays disabled until a limit is chosen deliberately.
  A financially meaningful concentration limit is a product decision; inventing
  one would be worse than an explicit absence. Required holder data being
  *absent* still blocks regardless.

Verdict precedence: a known violation outranks a missing fact. Both stop the
case, but "we measured this and it is dangerous" is the more precise statement
and is reported as such, with any gaps recorded alongside.

## Evidence mapping

| Decision | Evidence status | Domain verdicts |
| --- | --- | --- |
| CLEAR | AVAILABLE | all PASS |
| BLOCKED | **AVAILABLE** | affected domain FAIL, reason codes required |
| INSUFFICIENT_DATA | UNKNOWN | affected domain UNKNOWN |

The three domain fields say what *policy concluded*, not raw availability. Raw
per-domain availability, the verdict, blockers, gaps, chain ID, block number and
the snapshot digest are all preserved in `OnchainIntelligence`, so a future FUSE
reads structure rather than prose.

## Model role

ATLAS reuses the Phase 2C provider-neutral reasoning port. No second framework,
no tools, no Anthropic client inside the domain.

The model may summarize, flag patterns, and mark each finding as a verified fact
or an inference. Its output schema contains **no verdict field at all** — there
is nothing there for it to set. Semantic validation then refuses any address that
was not in the snapshot, and any acknowledged gap that contradicts an available
domain; a contradicted assessment is dropped entirely rather than partially
trusted.

**Model failure never changes the verdict.** If the provider times out, is
unavailable or returns malformed output, the deterministic safety state is
submitted without commentary. Safety does not depend on model uptime, and a
silent model never fabricates a blocker to fill the gap.

Instructions are versioned (`atlas-v1`) and hashed over the template text only.
Instructions and facts travel in separate request channels, so a hostile token
name or provider label stays a quoted string.

## Workflow behaviour

Only `AVAILABLE` + `ACCEPTED` satisfies the ATLAS prerequisite. Everything else —
blocked content, unknown, unavailable, invalid or stale — blocks the case, and
FUSE and COMMANDER cannot override it.

ATLAS evidence is safety-critical, so it participates in the risk-input digest.
Superseding it changes that digest, which invalidates any existing
`RISK_APPROVED` or `RISK_LIMITED` binding through the ordinary Phase 2A
revalidation path.

**Lifecycle note.** The ATLAS task is terminal once it succeeds, so a periodic
re-assessment needs a trigger that Phase 2D does not introduce. The supersession
semantics are implemented and tested; what is deferred is the scheduler that
would drive a refresh.

## Persistence

**No migration.** The verdict, blockers, gaps, per-domain availability, chain and
block provenance, snapshot digest and advisory commentary all fit the existing
`trade_case_evidence` JSONB payload as an optional `OnchainPayload.intelligence`.
Migrations `0001`-`0006` are untouched and `0006` remains head. No shadow on-chain
database is introduced; historical chain indexing is a separate concern.

## Capability boundary

ATLAS receives exactly `{lease, context, submit}`. The read port exposes one
method. The worker never holds an RPC client, an indexer client, a session, an
HTTP client, a signer, a wallet, an executor, a ledger writer, a SENTINEL call or
a status setter, and there is no generic tool surface. The deterministic
collector uses approved RPC services internally — that distinction is the point.

The RPC client gained exactly three explicit read methods (`eth_getCode`,
`eth_call` with a bare 4-byte selector, `eth_getStorageAt`) behind the existing
allowlist. There is no generic `rpc(method, params)` and no write or send path.
Chain identity is verified before any other call, and a mismatch fails closed.

## Configuration

`ATLAS_WORKER_ENABLED=false` by default, with `ATLAS_SNAPSHOT_MAX_AGE_SECONDS`.
Enabling ATLAS without the EVM runtime is rejected at configuration time rather
than producing a guaranteed unavailable contract domain. The safety policy itself
is code-defined and versioned, not environment-mutable, so it cannot drift
through an unaudited env change. No worker launcher is introduced: as in Phase
2C, nothing starts a worker implicitly.

## What Phase 2D does not implement

No SIGNAL, VECTOR, PULSE, ANCHOR, FUSE or COMMANDER. No executor, signer, wallet
or broadcast. ATLAS does not change SENTINEL semantics — its blocker happens
before any risk evaluation, and `RiskOutcome` and `RiskAuthorization` are
untouched. ANCHOR still owns execution liquidity, routing, slippage and maximum
safe size; ATLAS observes ownership and control facts only.

Deferred at the time: verified holder and creator providers for both chains, a
re-assessment trigger, and a concentration threshold chosen from real
requirements. Phase 2E delivers the providers; the re-assessment trigger and the
threshold remain open.
