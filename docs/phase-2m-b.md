# Phase 2M-B — Risk data completeness

The Phase 2M audit found three facts SENTINEL requires that no component
produced, and warned that asking SENTINEL anyway would be worse than not asking:
each gap classifies as a non-resizable `REJECT`, which is terminal and
permanently bars the market from ever opening another case. An architectural
hole would have spent a real decision.

This phase closes the gap without asking. It makes the facts available, gives
them provenance, and adds a server-side check that reports whether they are
there — and stops when they are not.

No second risk engine. No `TradeIntent`, no SENTINEL call, no fill.

## What exists now

- `HolderDistributionFacts` on `OnchainIntelligence` — the holder metrics ATLAS
  already derives, now persisted with the evidence.
- `src/orchestration/costs/` — a versioned contract for explicitly configured
  PAPER fee and slippage assumptions.
- `src/orchestration/riskdata/` — a read-only completeness check over the facts
  the existing SENTINEL contract actually consumes.
- Two settings: `paper_fee_bps` and `paper_slippage_bps`, both `None` by default.

No migration. Alembic head remains `0006`; the holder metrics live inside an
existing JSON payload column.

## 1. Holder facts, persisted

**A verdict is not a metric.** `holder_integrity == "PASS"` says the holder
domain met its data-quality prerequisites — it is not a concentration, not a
count, and no number can be recovered from it. The numbers existed inside the
ATLAS collector and never reached durable evidence, so a later reader had the
choice of re-running the collector or inferring a distribution from a verdict.
Both are wrong; the metrics are now recorded beside the verdict.

**The right metric, named rather than implied.** `top_ten_fraction` is the sum
of the ten largest balances over **on-chain total supply**, unadjusted — the
same figure ATLAS's own concentration policy judges, and the one SENTINEL's
`max_top_ten_holder_fraction` compares against. `measurement` states it on the
record so no later reader has to infer it from a field name.

The burn-adjusted variant travels separately as
`top_ten_fraction_excluding_burn`: it divides by *circulating* supply with
proven burn holdings removed, which is a different measure against a different
denominator. Substituting it for the raw figure would judge a smaller number,
and a safety limit must never be wrong in that direction.

**Provenance travels with every figure.** Source, the source's own observation
instant, what that instant refers to (`SOURCE_BLOCK` or `RESPONSE_TIME`), the
coverage proof (`COMPLETE` or `TOP_N_ONLY`), the snapshot block and its signed
distance from the chain block, the denominator as exact digits, and the
addresses the provider filtered out before we saw them.

`holder_count` carries `holder_count_basis = PROVIDER_REPORTED`, because it is
the provider's own count rather than something derived from the rows we
received. Labelling it is the difference between a number and a checkable one.

Nothing is recomputed here and no provider was integrated. These are the figures
the existing collector already produced, moved from a transient snapshot into
the durable record.

## 2. PAPER cost assumptions, explicitly separated

Two numbers decide how a simulated fill is priced. Both are **stated by an
operator**, and the contract says so in its own type: `basis` has exactly one
value, `OPERATOR_CONFIGURED_ASSUMPTION`.

Neither is defaulted. An unconfigured basis is not a free trade — a simulation
priced without being told what trading costs is optimistic by construction,
which is the one direction a paper result must never err in. Zero is a
meaningful *configured* value, which is precisely why absence rather than zero
is what signals a missing basis.

Meanings are pinned, because the alternative readings differ by factors:

- `fee_bps` — a proportional charge on the executed notional of **one side**. A
  later exit charges it again; a caller applying it twice to one fill would
  double-count.
- `slippage_bps` — the assumed adverse move between the reference price and the
  simulated fill. It moves the fill price and is never added to the fee.
- `excludes` names what the basis does not cover — gas, price impact beyond the
  assumed move, partial fills, failed transactions, the exit side — so two
  numbers are not mistaken for a complete cost model.

Four observed figures must never be read into these fields, and the contract has
no field any of them could arrive in: ANCHOR's execution deviation, a provider's
price impact, a tested capacity, and a realised fill cost. Nothing writes these
assumptions back into ANCHOR evidence — evidence records what was observed, and
an assumption stored as an observation would be indelible and wrong.

The policy ships bounds rather than amounts: five percent each, two orders of
magnitude above an ordinary fee or deviation, which makes it a typo guard rather
than a view on acceptable trading costs. The bounds travel with each reading by
content, not by version label.

## 3. The completeness check

`RiskDataReader` holds three read-only ports — the case and its evidence, the
recorded market layer, the durable stop — and no session, repository, quote
client or transport. The configured cost basis arrives as a value, so the
component cannot decide for itself what costs are assumed. The clock is read
after every `await`, never before.

`RiskFactKind` is derived from `src.risk.engine.evaluate` rather than from a
summary of the audit's three fields: every member is something that function
actually reads and can refuse on.

| input | source |
| --- | --- |
| reference price, token metadata, liquidity depth | recorded market observation |
| tradability, holder integrity, holder count, holder concentration | ATLAS on-chain evidence |
| routing availability | ANCHOR execution evidence |
| fee basis, slippage basis | operator-configured assumption |

The portfolio side — cash, exposure, position, daily loss, accounting — is
deliberately absent. It comes from the paper account, not from market data, and
is not this reader's subject.

Every reported fact carries its canonical asset, its unit and semantics in one
code, its source, when it was true according to that source, and when it stops
being usable. Token metadata is judged on the asset observation's **own**
instant, not the enclosing snapshot's, under its own stated bound. A configured assumption carries no observation time and no expiry,
because it was not observed at any instant and does not go stale.

**Incomplete is not rejected.** A missing fact produces a typed gap naming the
cause — not recorded, not established, stale, observed in the future, wrong
asset, not configured, coverage unproven, understated, or a verdict standing
where a metric should be. The reading stops there. It never becomes a SENTINEL
verdict.

**A gap is not a blocker.** Established negative evidence is reported alongside,
in both outcomes: blocked evidence from the **canonical safety sources**,
ATLAS's own blocker codes, a terminal or rejected case, and the system stop.
Which evidence is safety-critical is read from the workflow policy rather than
restated here, so the advisory layer cannot reach the blocker list — see the
hardening round below. An unreadable stop is reported as a blocker
rather than as silence, because unknown is not permission. A case can be both
incompletely measured and known to be dangerous, and the two are never collapsed.

Two judgements about holder data belong to the reader rather than the evidence:

- unproven coverage supports no concentration at all, however cleanly the source
  answered;
- a provider-filtered holder list leaves the numerator a lower bound while the
  denominator stays full supply, so the metric can only understate — usable as
  context, never as an input to a limit. An understated figure passing a limit
  is the one failure a limit exists to prevent.

**Complete is not permitted.** A complete reading says the checked data is
there. It says nothing about the requested size, the portfolio, the configured
limits or the system stops, all of which a real risk input also needs. No status
is mutated, no risk binding is written, no accounting is touched, and SIGNAL and
FUSE remain non-binding — neither appears among the sources at all.

## Compatibility

Evidence is append-only and its submission fingerprints are stored, so the new
holder block is a change to bytes already in the database. An absent key and a
key holding `null` are different bytes, and emitting one for a row that never
had it would break replay while parsing perfectly.

The shape as read is therefore remembered and reproduced — the same mechanism
`ExecutionAssessmentDetail` and `SynthesisDetail` already use for their legacy
timestamp, applied once more rather than generalised into a serialisation layer
nobody could reason about. A payload that arrived without the key is written
back without it; a run that looked and found nothing records an explicit `null`,
which is a different fact and stays distinguishable.

The proof fixtures were produced by running the model as it stood at `779261d`,
the merge this branch is based on. Unknown fields are still refused:
compatibility is about reproducing what was written, never about accepting more.

## Hardening round

Two defects, each reproduced against `215b56e` before being fixed.

**An advisory opinion arrived as a risk blocker.** The blocker collection walked
every evidence type, so a FUSE synthesis carrying hard blockers produced
`FUSE_EVIDENCE_BLOCKED` while the canonical safety sources were entirely
unchanged. That is precisely the authority the advisory layer is kept out of the
risk-input digest to deny it, granted indirectly where nobody would look for it.

The collection is now bound to `WorkflowPolicy.safety_types` — the requirement
table that already decides the question — rather than to a second list of its
own. SYNTHESIS, SENTIMENT and DISCOVERY are absent from it, so neither FUSE nor
SIGNAL can produce a risk blocker, while ATLAS's blocker codes and a measured
on-chain violation still get through unchanged.

**Token metadata was carried without an age check.** `_market_facts` accepted
the base asset's symbol and decimals with no freshness test and no
`valid_until`, so a snapshot with a fresh price and fresh liquidity but
day-old asset metadata reported `RISK_DATA_COMPLETE` with no gap. That is a
valid market model, not a contrived one: a nested observation may be older than
its parent, and reading the snapshot's own instant made the older fact look as
fresh as the newer one it travelled with.

`RiskDataPolicy` now states `max_token_metadata_age` as its own field, judged on
`source_observed_at`, feeding a `valid_until` that enters the reading's own
validity. The bound is deliberately a separate field rather than a reuse:
SENTINEL checks the token snapshot's age independently of the market snapshot's,
and a bound inherited by accident would be a contract nobody chose. Its value
matches the price bound because the market layer re-observes pair metadata as
part of the same snapshot at the same cadence, so a tighter one would refuse
every reading the recorder can produce rather than catch anything.

It is **necessary and not sufficient**: SENTINEL applies its own configurable
`max_snapshot_age_seconds`, tighter by default, when it evaluates. A complete
reading here is not a promise that a later evaluation will accept the same data;
what it guarantees is the other direction, that nothing stale by the recorder's
own cadence is reported as present.

## Test evidence

85 tests in `tests/riskdata/`, none of them an identifier scan:

- the **real ATLAS worker** end to end — deterministic snapshot builder,
  versioned policy, handler, workflow service — asserting the stored metrics
  equal what the real `concentration()` derives from the same rows, that
  provenance and coverage travel with them, that an unavailable source records
  an explicit absence, and that the raw and burn-adjusted figures stay distinct;
- historical evidence: byte-identical re-serialisation, original fingerprints,
  and unchanged replay through the real workflow service;
- holder semantics: verdict without metric, unproven coverage, provider
  exclusions, a missing count, a future observation, an unestablished domain;
- stale, superseded, future-dated and wrongly attributed sources, through real
  supersession rather than copied identifiers;
- complete data against a missing or half-configured cost basis;
- known blockers reported together with data gaps;
- cost assumptions bounded, PAPER-only, configured, and unreachable from ANCHOR;
- a complete reading writing nothing: no status change, no evidence, no risk
  binding;
- the hardening round: identical gaps and blockers with a negative advisory
  synthesis present and absent, no indirect SIGNAL effect, the blocker scope
  proved to be *read* from the workflow policy by narrowing that policy and
  watching the blocker disappear, direct safety blockers surviving it, and
  metadata freshness on valid market models — own source time, the boundary
  before, on and after expiry, a future observation, and validity propagation.

## Remaining gaps

- **Nothing consumes the reading.** The `RiskInputBuilder` that would assemble a
  `core.MarketSnapshot` and a `RiskContext` from these facts is the next step,
  and the portfolio half of that input is not checked here at all.
- **A durable case-to-order-to-fill link** and its schema remain undecided, as
  does resumption after a terminal executed status. See `docs/phase-2m-a.md`.
- **Shared system stops** must be wired before the first integrated fill.
  Nothing in the TradeCase flow can currently *set* `paper_accounts.paused`; this
  reader can only observe it.
- **The metadata bound is looser than SENTINEL's default.** Ninety seconds
  against thirty: data can be complete here and still be refused at evaluation.
  Tightening it would require the market layer to re-observe pair metadata more
  often than it records snapshots, which no configuration available today does.
- **`holder_count` remains provider-reported.** Nothing verifies it against the
  rows received, and with `TOP_N_ONLY` coverage nothing could.
- **`RESPONSE_TIME` holder provenance** is accepted by the current ATLAS policy
  and cannot detect a lagging indexer. That pre-live invariant is unchanged here.
- **No launcher.** No worker is started by anything, and configuring a cost
  basis or a notional enables nothing.
- Exits, the PAPER cost model's fidelity beyond these two assumptions, and live
  execution all remain out of scope.
