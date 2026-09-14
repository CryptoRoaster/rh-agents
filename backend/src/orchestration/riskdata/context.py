"""The narrow read surface the completeness check is built on.

Three read-only ports and nothing else: the case and its evidence, the recorded
market layer, and the durable system stop. No session, no repository, no
provider client, no transport, and no method that could write anything. The
configured cost basis arrives already read, as a value rather than as a source
this component could go and consult on its own terms.

Nothing here evaluates risk. It does not compare a liquidity figure against a
limit, judge a concentration, or form any view about a market — it establishes
whether the inputs such a judgement would need are present, current and
correctly attributed, and hands the judgement itself to the one component that
owns it.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from src.core.clock import Clock, SystemClock
from src.markets.models import Availability, MarketSnapshot
from src.orchestration.commander.context import SystemPausePort, SystemPauseUnavailable
from src.orchestration.costs.models import PaperCostAssumptions, PaperCostReading
from src.orchestration.riskdata.models import (
    RiskDataBlocker,
    RiskDataGap,
    RiskDataGapCode,
    RiskDataOutcome,
    RiskDataReadiness,
    RiskFact,
    RiskFactKind,
    RiskFactOrigin,
)
from src.orchestration.riskdata.policy import RISK_DATA_V1, RiskDataPolicy
from src.orchestration.sizing.context import base_asset_metadata, reference_price
from src.orchestration.workflow.engine import active_evidence
from src.orchestration.workflow.models import (
    TERMINAL_CASE_STATUSES,
    EvidenceAcceptance,
    EvidenceEnvelope,
    EvidenceStatus,
    EvidenceType,
    HolderDistributionFacts,
    LiquidityExecutionPayload,
    OnchainPayload,
    TradeCase,
    TradeCaseStatus,
    WorkflowFailure,
)

MEANINGS: dict[RiskFactKind, str] = {
    RiskFactKind.REFERENCE_PRICE: "USD_PER_BASE_UNIT",
    RiskFactKind.TOKEN_METADATA: "SYMBOL_AND_DECIMALS",
    RiskFactKind.TOKEN_TRADABILITY: "SAFETY_STATUS",
    RiskFactKind.LIQUIDITY_DEPTH: "USD_POOL_LIQUIDITY",
    RiskFactKind.ROUTING_AVAILABILITY: "SAFETY_STATUS",
    RiskFactKind.EXECUTION_FEE_BASIS: "BASIS_POINTS_ON_ONE_SIDE_NOTIONAL",
    RiskFactKind.EXECUTION_SLIPPAGE_BASIS: "BASIS_POINTS_ADVERSE_MOVE",
    RiskFactKind.HOLDER_COUNT: "PROVIDER_REPORTED_HOLDER_COUNT",
    RiskFactKind.HOLDER_CONCENTRATION: "TOP_TEN_FRACTION_OF_TOTAL_SUPPLY",
    RiskFactKind.HOLDER_INTEGRITY: "SAFETY_STATUS",
}

# Holder coverage proofs a concentration may be derived from. `UNKNOWN` is
# absent deliberately: a source that could not establish what it saw supports no
# metric at all, however cleanly it answered.
PROVEN_COVERAGE = frozenset({"COMPLETE", "TOP_N_ONLY"})


class RiskDataCaseSource(Protocol):
    async def get_trade_case(self, trade_case_id: UUID) -> TradeCase: ...

    async def evidence(self, trade_case_id: UUID) -> tuple[EvidenceEnvelope, ...]: ...


class RiskDataMarketInput(Protocol):
    async def latest(
        self, identity: str, *, include_fixtures: bool = False
    ) -> MarketSnapshot | None: ...


class RiskDataUnavailable(Exception):
    """The case could not be read at all. Carries a safe reason code only."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class RiskDataReader:
    """Assembles the state of the inputs a later risk evaluation will need."""

    cases: RiskDataCaseSource
    markets: RiskDataMarketInput
    # The configured PAPER cost basis, already read. Passed as a value so this
    # component cannot decide for itself what costs are assumed.
    costs: PaperCostReading
    policy: RiskDataPolicy = RISK_DATA_V1
    clock: Clock = SystemClock()
    # Supplied by a deployment that also runs the accounting subsystem, which is
    # where the durable stop lives. Absent means unreadable, and unreadable is
    # not permission — it is reported as a blocker rather than as silence.
    pause: SystemPausePort | None = None
    include_fixtures: bool = False

    async def readiness(self, trade_case_id: UUID) -> RiskDataReadiness:
        try:
            trade_case = await self.cases.get_trade_case(trade_case_id)
        except WorkflowFailure as error:
            raise RiskDataUnavailable("TRADE_CASE_NOT_FOUND") from error
        try:
            current = active_evidence(await self.cases.evidence(trade_case_id))
        except WorkflowFailure as error:
            raise RiskDataUnavailable("EVIDENCE_INTEGRITY") from error

        snapshot = await self.markets.latest(
            trade_case.market.pair_id, include_fixtures=self.include_fixtures
        )
        paused, pause_readable = await self._stop()

        # Read after every await, never before: a freshness bound measured at
        # the start of the work is not the bound the answer is used under.
        now = self.clock.now()
        base_asset_id = trade_case.market.base_asset_id

        facts: list[RiskFact] = []
        gaps: list[RiskDataGap] = []
        blockers: list[RiskDataBlocker] = []

        _market_facts(snapshot, base_asset_id, now, self.policy, facts, gaps)
        _cost_facts(self.costs, facts, gaps)
        _routing_fact(current.get(EvidenceType.LIQUIDITY_EXECUTION), now, facts, gaps)
        _onchain_facts(current.get(EvidenceType.ONCHAIN), base_asset_id, now, facts, gaps, blockers)
        _established_blockers(current, now, blockers)
        _control_blockers(trade_case, paused, pause_readable, blockers)

        horizons = [item.valid_until for item in facts if item.valid_until is not None]
        ahead = [horizon for horizon in horizons if horizon > now]
        return RiskDataReadiness(
            outcome=None if gaps else RiskDataOutcome.RISK_DATA_COMPLETE,
            policy_version=self.policy.version,
            trade_case_id=trade_case_id,
            base_asset_id=base_asset_id,
            trade_case_status=trade_case.status,
            facts=tuple(sorted(facts, key=lambda item: item.kind.value)),
            gaps=tuple(sorted(gaps, key=lambda item: item.kind.value)),
            blockers=tuple(blockers),
            observed_at=now,
            valid_until=min(ahead) if ahead else None,
        )

    async def _stop(self) -> tuple[bool, bool]:
        """The durable stop, and whether it could be read at all."""
        if self.pause is None:
            return True, False
        try:
            return await self.pause.system_paused(), True
        except SystemPauseUnavailable:
            return True, False


def _add(
    facts: list[RiskFact],
    kind: RiskFactKind,
    origin: RiskFactOrigin,
    *,
    source: str,
    asset_id: str | None = None,
    observed_at: datetime | None = None,
    valid_until: datetime | None = None,
) -> None:
    facts.append(
        RiskFact(
            kind=kind,
            origin=origin,
            meaning=MEANINGS[kind],
            asset_id=asset_id,
            source=source,
            observed_at=observed_at,
            valid_until=valid_until,
        )
    )


def _gap(
    gaps: list[RiskDataGap], kind: RiskFactKind, code: RiskDataGapCode, origin: RiskFactOrigin
) -> None:
    gaps.append(RiskDataGap(kind=kind, code=code, expected_origin=origin))


def _market_facts(
    snapshot: MarketSnapshot | None,
    base_asset_id: str,
    now: datetime,
    policy: RiskDataPolicy,
    facts: list[RiskFact],
    gaps: list[RiskDataGap],
) -> None:
    """Price, token metadata and pool depth, from one recorded observation."""
    origin = RiskFactOrigin.RECORDED_MARKET_OBSERVATION
    kinds = (
        RiskFactKind.REFERENCE_PRICE,
        RiskFactKind.TOKEN_METADATA,
        RiskFactKind.LIQUIDITY_DEPTH,
    )
    if snapshot is None:
        for kind in kinds:
            _gap(gaps, kind, RiskDataGapCode.NOT_RECORDED, origin)
        return

    price = reference_price(snapshot)
    if price is None:
        _gap(gaps, RiskFactKind.REFERENCE_PRICE, RiskDataGapCode.NOT_ESTABLISHED, origin)
    elif price.asset_id != base_asset_id:
        # A price from the wrong side of a pair is wrong by the exchange rate
        # and looks entirely plausible, so identity is checked rather than
        # inferred from the fact that a price arrived.
        _gap(gaps, RiskFactKind.REFERENCE_PRICE, RiskDataGapCode.WRONG_ASSET, origin)
    else:
        code = _freshness(price.observed_at, now, policy.max_price_age)
        if code is not None:
            _gap(gaps, RiskFactKind.REFERENCE_PRICE, code, origin)
        else:
            _add(
                facts,
                RiskFactKind.REFERENCE_PRICE,
                origin,
                source=price.provider,
                asset_id=price.asset_id,
                observed_at=price.observed_at,
                valid_until=price.observed_at + policy.max_price_age,
            )

    metadata = base_asset_metadata(snapshot)
    if metadata is None:
        # Decimals were never recorded. Never defaulted to eighteen: that
        # assumption is wrong by orders of magnitude on a six-decimal token.
        _gap(gaps, RiskFactKind.TOKEN_METADATA, RiskDataGapCode.NOT_ESTABLISHED, origin)
    elif metadata.asset_id != base_asset_id:
        _gap(gaps, RiskFactKind.TOKEN_METADATA, RiskDataGapCode.WRONG_ASSET, origin)
    else:
        _add(
            facts,
            RiskFactKind.TOKEN_METADATA,
            origin,
            source=metadata.source_provider,
            asset_id=metadata.asset_id,
            observed_at=metadata.source_observed_at,
        )

    liquidity = snapshot.liquidity
    if liquidity.status != Availability.AVAILABLE or liquidity.value_usd is None:
        _gap(gaps, RiskFactKind.LIQUIDITY_DEPTH, RiskDataGapCode.NOT_ESTABLISHED, origin)
    elif liquidity.asset_id != base_asset_id:
        _gap(gaps, RiskFactKind.LIQUIDITY_DEPTH, RiskDataGapCode.WRONG_ASSET, origin)
    else:
        code = _freshness(liquidity.observed_at, now, policy.max_price_age)
        if code is not None:
            _gap(gaps, RiskFactKind.LIQUIDITY_DEPTH, code, origin)
        else:
            _add(
                facts,
                RiskFactKind.LIQUIDITY_DEPTH,
                origin,
                source=liquidity.provider,
                asset_id=liquidity.asset_id,
                observed_at=liquidity.observed_at,
                valid_until=liquidity.observed_at + policy.max_price_age,
            )


def _cost_facts(costs: PaperCostReading, facts: list[RiskFact], gaps: list[RiskDataGap]) -> None:
    """The configured simulation cost basis, marked as an assumption."""
    origin = RiskFactOrigin.OPERATOR_CONFIGURED_ASSUMPTION
    kinds = (RiskFactKind.EXECUTION_FEE_BASIS, RiskFactKind.EXECUTION_SLIPPAGE_BASIS)
    if not isinstance(costs, PaperCostAssumptions):
        for kind in kinds:
            _gap(gaps, kind, RiskDataGapCode.NOT_CONFIGURED, origin)
        return
    for kind in kinds:
        # No `observed_at` and no expiry: a stated assumption was not observed
        # at any instant and does not go stale. Recording a read time here would
        # dress configuration up as a measurement.
        _add(facts, kind, origin, source=costs.policy.version)


def _routing_fact(
    anchor: EvidenceEnvelope | None,
    now: datetime,
    facts: list[RiskFact],
    gaps: list[RiskDataGap],
) -> None:
    """Whether an executable route was actually established for this market."""
    origin = RiskFactOrigin.ANCHOR_EXECUTION_EVIDENCE
    kind = RiskFactKind.ROUTING_AVAILABILITY
    if anchor is None:
        _gap(gaps, kind, RiskDataGapCode.NOT_ESTABLISHED, origin)
        return
    effective = anchor.effective_status(now)
    if effective is EvidenceStatus.STALE:
        _gap(gaps, kind, RiskDataGapCode.STALE, origin)
        return
    if effective is EvidenceStatus.INVALID:
        _gap(gaps, kind, RiskDataGapCode.OBSERVED_IN_THE_FUTURE, origin)
        return
    payload = anchor.payload
    if effective is not EvidenceStatus.AVAILABLE or not isinstance(
        payload, LiquidityExecutionPayload
    ):
        _gap(gaps, kind, RiskDataGapCode.NOT_ESTABLISHED, origin)
        return
    detail = payload.execution
    established = (
        detail is not None
        and detail.capacity_semantics != "UNKNOWN"
        and detail.largest_tested_acceptable_notional_usd is not None
    ) or (
        # Evidence written before Phase 2J carries no execution detail. A legacy
        # row that named its route and a size it accepted established the same
        # thing, and is read as such rather than discarded.
        detail is None
        and payload.routing_provenance is not None
        and payload.maximum_safe_size_usd is not None
    )
    if not established:
        _gap(gaps, kind, RiskDataGapCode.NOT_ESTABLISHED, origin)
        return
    _add(
        facts,
        kind,
        origin,
        source=anchor.provenance.source,
        observed_at=anchor.observed_at,
        valid_until=anchor.valid_until,
    )


def _onchain_facts(
    atlas: EvidenceEnvelope | None,
    base_asset_id: str,
    now: datetime,
    facts: list[RiskFact],
    gaps: list[RiskDataGap],
    blockers: list[RiskDataBlocker],
) -> None:
    """Tradability, holder integrity and the two holder numbers behind it."""
    origin = RiskFactOrigin.ATLAS_ONCHAIN_EVIDENCE
    kinds = (
        RiskFactKind.TOKEN_TRADABILITY,
        RiskFactKind.HOLDER_INTEGRITY,
        RiskFactKind.HOLDER_COUNT,
        RiskFactKind.HOLDER_CONCENTRATION,
    )
    if atlas is None:
        for kind in kinds:
            _gap(gaps, kind, RiskDataGapCode.NOT_ESTABLISHED, origin)
        return
    effective = atlas.effective_status(now)
    if effective in (EvidenceStatus.STALE, EvidenceStatus.INVALID):
        code = (
            RiskDataGapCode.STALE
            if effective is EvidenceStatus.STALE
            else RiskDataGapCode.OBSERVED_IN_THE_FUTURE
        )
        for kind in kinds:
            _gap(gaps, kind, code, origin)
        return
    payload = atlas.payload
    if effective is not EvidenceStatus.AVAILABLE or not isinstance(payload, OnchainPayload):
        # An envelope the workflow does not consider available supplies no risk
        # fact, whatever its payload happens to contain. The distinction between
        # "unavailable" and "dangerous" is kept by the blocker list, not by
        # reading values out of an envelope that says it established nothing.
        for kind in kinds:
            _gap(gaps, kind, RiskDataGapCode.NOT_ESTABLISHED, origin)
        return

    source = atlas.provenance.source
    for kind, verdict in (
        (RiskFactKind.TOKEN_TRADABILITY, payload.contract_integrity),
        (RiskFactKind.HOLDER_INTEGRITY, payload.holder_integrity),
    ):
        if verdict == "UNKNOWN":
            # Nothing was established, so there is no fact — not even a negative
            # one. An unmeasured domain is a gap.
            _gap(gaps, kind, RiskDataGapCode.NOT_ESTABLISHED, origin)
            continue
        # PASS and FAIL are both established facts. A FAIL travels as a fact
        # *and* as a blocker: a risk evaluation needs the value, and an operator
        # needs to see that it is the dangerous one.
        _add(
            facts,
            kind,
            origin,
            source=source,
            asset_id=base_asset_id,
            observed_at=atlas.observed_at,
            valid_until=atlas.valid_until,
        )

    intelligence = payload.intelligence
    holders = None if intelligence is None else intelligence.holders
    if holders is None:
        # The verdict may well be PASS. A verdict is not a metric, and this is
        # precisely the substitution that must never happen silently: it says
        # the holder domain met its prerequisites, not how concentrated the
        # token is, and no number can be recovered from it.
        code = (
            RiskDataGapCode.VERDICT_WITHOUT_METRIC
            if payload.holder_integrity != "UNKNOWN"
            else RiskDataGapCode.NOT_ESTABLISHED
        )
        _gap(gaps, RiskFactKind.HOLDER_COUNT, code, origin)
        _gap(gaps, RiskFactKind.HOLDER_CONCENTRATION, code, origin)
        return
    _holder_facts(holders, atlas, base_asset_id, now, facts, gaps)


def _holder_facts(
    holders: HolderDistributionFacts,
    atlas: EvidenceEnvelope,
    base_asset_id: str,
    now: datetime,
    facts: list[RiskFact],
    gaps: list[RiskDataGap],
) -> None:
    origin = RiskFactOrigin.ATLAS_ONCHAIN_EVIDENCE
    if holders.observed_at > now:
        for kind in (RiskFactKind.HOLDER_COUNT, RiskFactKind.HOLDER_CONCENTRATION):
            _gap(gaps, kind, RiskDataGapCode.OBSERVED_IN_THE_FUTURE, origin)
        return

    if holders.holder_count is None:
        _gap(gaps, RiskFactKind.HOLDER_COUNT, RiskDataGapCode.NOT_ESTABLISHED, origin)
    else:
        _add(
            facts,
            RiskFactKind.HOLDER_COUNT,
            origin,
            source=holders.source,
            asset_id=base_asset_id,
            observed_at=holders.observed_at,
            valid_until=atlas.valid_until,
        )

    kind = RiskFactKind.HOLDER_CONCENTRATION
    if holders.completeness not in PROVEN_COVERAGE:
        _gap(gaps, kind, RiskDataGapCode.HOLDER_COVERAGE_UNPROVEN, origin)
    elif holders.provider_excluded_addresses:
        # The numerator is missing rows the provider filtered while the
        # denominator stays full on-chain supply, so the figure can only
        # understate. Usable as context and not as an input to a limit.
        _gap(gaps, kind, RiskDataGapCode.HOLDER_METRIC_UNDERSTATED, origin)
    elif holders.top_ten_fraction is None:
        _gap(gaps, kind, RiskDataGapCode.NOT_ESTABLISHED, origin)
    else:
        _add(
            facts,
            kind,
            origin,
            source=holders.source,
            asset_id=base_asset_id,
            observed_at=holders.observed_at,
            valid_until=atlas.valid_until,
        )


def _established_blockers(
    current: dict[EvidenceType, EvidenceEnvelope],
    now: datetime,
    blockers: list[RiskDataBlocker],
) -> None:
    """Negative evidence the specialists actually established.

    Reported whether or not anything is missing. A case can be simultaneously
    incompletely measured and known to be dangerous, and collapsing the second
    into the first would let a data gap look like the only problem.
    """
    for evidence_type, item in sorted(current.items(), key=lambda pair: pair[0].value):
        if item.effective_status(now) != EvidenceStatus.AVAILABLE:
            continue
        if item.payload.acceptance() is EvidenceAcceptance.BLOCKED:
            blockers.append(
                RiskDataBlocker(
                    code=f"{item.producer_role.value}_EVIDENCE_BLOCKED",
                    origin=(
                        RiskFactOrigin.ATLAS_ONCHAIN_EVIDENCE
                        if evidence_type is EvidenceType.ONCHAIN
                        else RiskFactOrigin.ANCHOR_EXECUTION_EVIDENCE
                        if evidence_type is EvidenceType.LIQUIDITY_EXECUTION
                        else None
                    ),
                    evidence_id=item.evidence_id,
                )
            )
        payload = item.payload
        if isinstance(payload, OnchainPayload) and payload.intelligence is not None:
            for code in payload.intelligence.blockers:
                blockers.append(
                    RiskDataBlocker(
                        code=code,
                        origin=RiskFactOrigin.ATLAS_ONCHAIN_EVIDENCE,
                        evidence_id=item.evidence_id,
                    )
                )


def _control_blockers(
    trade_case: TradeCase,
    paused: bool,
    pause_readable: bool,
    blockers: list[RiskDataBlocker],
) -> None:
    """Decisions and stops that already stand, never re-derived here."""
    if trade_case.status is TradeCaseStatus.RISK_REJECTED:
        blockers.append(RiskDataBlocker(code="RISK_REJECTED"))
    elif trade_case.status in TERMINAL_CASE_STATUSES:
        blockers.append(RiskDataBlocker(code="TRADE_CASE_TERMINAL"))
    if not pause_readable:
        # An unreadable stop is unknown, and unknown is not permission.
        blockers.append(RiskDataBlocker(code="SYSTEM_STOP_UNREADABLE"))
    elif paused:
        blockers.append(RiskDataBlocker(code="SYSTEM_PAUSED"))


def _freshness(
    observed_at: datetime, now: datetime, tolerance: timedelta
) -> RiskDataGapCode | None:
    """Whether an observation is usable now, half-open at the far boundary."""
    if observed_at > now:
        return RiskDataGapCode.OBSERVED_IN_THE_FUTURE
    if now >= observed_at + tolerance:
        return RiskDataGapCode.STALE
    return None
