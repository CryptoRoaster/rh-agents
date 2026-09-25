"""The one narrow server-side call that may ask SENTINEL about a TradeCase.

Everything happens in a single transaction, in the order the rest of this system
already established: paper account, then trade case. That ordering is not
convenience — the portfolio a verdict was reached from and the binding that
records it have to commit together, or a crash between them leaves a decision
bound to a state nobody can reconstruct.

A caller supplies a case id and a request key. It cannot supply a `RiskInput`, a
price, a size, a limit, a portfolio, a session or a provider client. Every one of
those is assembled here from the source that owns it, which is what makes "a
worker cannot choose what SENTINEL is shown" a property of the call rather than a
rule somebody has to keep.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.core.models import (
    ExecutionTiming,
    HolderSnapshot,
    LiquiditySnapshot,
    MarketSnapshot,
    RiskLimits,
    RiskOutcome,
    SafetyStatus,
    Side,
    TokenSnapshot,
    TradeIntent,
    TradingMode,
)
from src.core.numbers import quantize, quantize_down
from src.data.repository import aware, read_position
from src.data.tables import AccountRow, PositionRow, TradeCaseRiskRequestRow
from src.ledger.portfolio import (
    PortfolioState,
    portfolio_basis,
    portfolio_state,
    roll_loss_day,
)
from src.markets.models import Availability
from src.markets.models import MarketSnapshot as RecordedMarket
from src.orchestration.commander.context import SystemPausePort
from src.orchestration.costs.models import PaperCostAssumptions, PaperCostReading
from src.orchestration.riskdata.context import RiskDataReader
from src.orchestration.riskdata.models import RiskDataReadiness
from src.orchestration.riskrequest.models import (
    RiskRequestEvaluated,
    RiskRequestReading,
    RiskRequestRefusal,
    RiskRequestRefused,
    risk_request_digest,
)
from src.orchestration.sizing.context import PaperSizingReader
from src.orchestration.sizing.models import (
    BaseAssetMetadata,
    ReferencePrice,
    SizingAssessment,
    SizingRefusal,
)
from src.orchestration.valuation.models import PortfolioValuation, unvaluable_reason
from src.orchestration.valuation.service import PositionValuationReader
from src.orchestration.workflow.engine import active_evidence
from src.orchestration.workflow.models import (
    TERMINAL_CASE_STATUSES,
    EvidenceEnvelope,
    EvidenceType,
    LiquidityExecutionPayload,
    OnchainPayload,
    TradeCase,
    TradeCaseStatus,
    WorkflowFailure,
)
from src.orchestration.workflow.service import TradeCaseService, case_from_row
from src.risk.authorization import RiskAuthorization, classify_decision
from src.risk.engine import evaluate

# The two verdicts this system maps onto SENTINEL's safety status. `UNKNOWN` is
# deliberately absent: an unestablished domain is a completeness gap the
# readiness check already refuses on, never a value handed to a risk evaluation.
SAFETY_STATUS = {"PASS": SafetyStatus.PASS, "FAIL": SafetyStatus.FAIL}


class RiskRequestUnavailable(Exception):
    """The request could not be attempted at all. Safe reason code only.

    Raised for states that are not a fact about this case — an uninitialised
    paper account, a case that does not exist, or an internal disagreement
    between the completeness check and the values extracted after it.
    """

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class OneSnapshot:
    """One market read per request, shared by everything that consumes it.

    The completeness check, the sizing calculation and the value extraction must
    all describe the same observation. Letting each call `latest()` separately
    would allow a newer row to arrive between them, and the binding would record
    a basis no single instant ever held.
    """

    def __init__(self, markets: Any, include_fixtures: bool) -> None:
        self._markets = markets
        self._include_fixtures = include_fixtures
        self._seen: dict[str, RecordedMarket | None] = {}

    async def latest(
        self, identity: str, *, include_fixtures: bool = False
    ) -> RecordedMarket | None:
        if identity not in self._seen:
            self._seen[identity] = await self._markets.latest(
                identity, include_fixtures=include_fixtures or self._include_fixtures
            )
        return self._seen[identity]


@dataclass(frozen=True)
class CanonicalInputs:
    """Every value SENTINEL will see, with the source each came from."""

    readiness: RiskDataReadiness
    sizing: SizingAssessment
    costs: PaperCostAssumptions
    snapshot: RecordedMarket
    onchain: EvidenceEnvelope
    anchor: EvidenceEnvelope
    # Every current envelope, keyed by evidence type, for the audit record.
    canonical_evidence: dict[str, EvidenceEnvelope]


@dataclass(frozen=True)
class RiskRequestService:
    """Assembles a canonical risk input and binds the verdict to it."""

    sessions: async_sessionmaker[AsyncSession]
    cases: TradeCaseService
    markets: Any
    # The configured PAPER cost basis and the configured entry size, both read
    # before the call. Neither is this service's to choose, and neither is
    # defaulted here.
    costs: PaperCostReading
    requested_notional_usd: Decimal | None
    # SENTINEL's own limits, obtained server-side and recorded with the request.
    limits: RiskLimits
    trading_mode: TradingMode = TradingMode.OBSERVE
    kill_switch: bool = False
    # Supplied by a deployment that also runs the accounting subsystem. Its
    # presence says the stop is configured; its *value* is never consulted here,
    # because the locked account row is the authoritative read.
    pause: SystemPausePort | None = None
    clock: Clock = SystemClock()
    include_fixtures: bool = False

    async def request_risk_evaluation(
        self,
        trade_case_id: UUID,
        *,
        request_key: str,
        expected_revision: int | None = None,
        expected_risk_input_digest: str | None = None,
    ) -> RiskRequestReading:
        """Ask SENTINEL about one case, once, and keep the whole basis."""
        feed = OneSnapshot(self.markets, self.include_fixtures)
        # History needs no current prices. Checked before anything is valued, so
        # replaying a stored verdict never depends on the market layer being
        # reachable. The authoritative replay check still happens under the lock.
        if await self._already_evaluated(trade_case_id):
            valuation = PortfolioValuation()
        else:
            valuation = await self._value_portfolio(feed)
        # Every open holding is priced before the account lock is taken. The
        # reads are bounded, but holding a portfolio-wide lock across an
        # injected port is a latency somebody else pays for. What that costs is
        # the chance of a position appearing in between, and the check under the
        # lock below closes it.
        async with self.sessions.begin() as session:
            account = await session.scalar(
                select(AccountRow).where(AccountRow.id == 1).with_for_update()
            )
            if account is None:
                raise RiskRequestUnavailable("PAPER_ACCOUNT_NOT_INITIALISED")
            try:
                row = await self.cases._locked_case(session, trade_case_id)
            except WorkflowFailure:
                raise RiskRequestUnavailable("TRADE_CASE_NOT_FOUND") from None
            trade_case = case_from_row(row)

            stored = await session.scalar(
                select(TradeCaseRiskRequestRow).where(
                    TradeCaseRiskRequestRow.trade_case_id == trade_case_id
                )
            )
            if stored is not None:
                return _replay(stored, trade_case.status, request_key)

            assessed = await self._assess(
                feed,
                trade_case,
                expected_revision,
                expected_risk_input_digest,
                # The durable stop, read from the row this transaction already
                # holds locked. Going back through the port would be an
                # unsynchronised snapshot read, and any check-then-act on one
                # leaves a window a concurrent pause can commit inside.
                paused=bool(account.paused),
            )
            if isinstance(assessed, RiskRequestRefused):
                return assessed

            positions = [
                read_position(item) for item in (await session.scalars(select(PositionRow))).all()
            ]
            held = {item.asset_id for item in positions if item.quantity != 0}
            appeared = valuation.unconsidered(held)
            if appeared:
                # The portfolio moved while it was being valued, so the figure
                # SENTINEL would judge covers only part of it — worse than no
                # figure, because it looks like one. A holding that *was* looked
                # at and could not be priced is a different failure, answered
                # below on the marks as they stand at the decision instant.
                return _refused(
                    trade_case,
                    RiskRequestRefusal.PORTFOLIO_CHANGED_DURING_VALUATION,
                    assessed.readiness,
                )
            workflow = await self.cases.workflow_inputs_in_session(session, row)

            # The last clock read, after the last input read. Everything from
            # here to the verdict is synchronous, so one instant governs
            # eligibility, the validity of the basis, every source age, the UTC
            # loss day and SENTINEL itself. An instant taken earlier describes
            # when the loading began, and a case can lapse while it runs.
            now = self.clock.now()
            roll_loss_day(account, now)

            # What the case *is* at the decision instant, not what the last
            # write recorded. A stored status is a snapshot and time moves
            # without writes: a case whose lifetime lapsed or whose trigger aged
            # out keeps READY_FOR_RISK until something touches the row, and
            # `_stabilize` runs after the binding is written — far too late to
            # be a precondition. The same central evaluator answers, on inputs
            # already loaded under the locks, without awaiting anything.
            effective = self.cases.evaluate_inputs(workflow, now)
            if effective.status is not TradeCaseStatus.READY_FOR_RISK:
                return _refused(
                    trade_case,
                    RiskRequestRefusal.TRADE_CASE_NO_LONGER_ELIGIBLE,
                    assessed.readiness,
                    detail=effective.status.value,
                )
            if effective.risk_input_digest != trade_case.risk_input_digest:
                return _refused(
                    trade_case,
                    RiskRequestRefusal.SOURCE_CHANGED_DURING_REQUEST,
                    assessed.readiness,
                )
            # And whether the basis itself survived the reads. Both readings were
            # taken before the locks settled; each carries the horizon its own
            # sources give it, and neither is extended here.
            if not assessed.readiness.is_current_at(now) or not assessed.sizing.is_current_at(now):
                return _refused(
                    trade_case,
                    RiskRequestRefusal.DECISION_BASIS_EXPIRED,
                    assessed.readiness,
                )

            market = risk_market(
                base_asset_id=assessed.sizing.base_asset_id,
                price=assessed.sizing.reference_price,
                base_asset=assessed.sizing.base_asset,
                snapshot=assessed.snapshot,
                onchain=assessed.onchain,
                anchor=assessed.anchor,
                costs=assessed.costs,
                correlation_id=trade_case.correlation_id,
                identity_key=request_key,
            )
            stale = too_old_for(market, now, self.limits)
            if stale is not None:
                # Present, provable and still older than SENTINEL's own bound.
                # Asking anyway would come back as a terminal rejection of the
                # market, spending a real decision on a stale reading.
                return _refused(
                    trade_case,
                    RiskRequestRefusal.SOURCE_OLDER_THAN_RISK_LIMIT,
                    assessed.readiness,
                    detail=stale,
                )

            state = portfolio_state(
                cash_usd=account.cash_usd,
                realized_loss_today_usd=account.realized_loss_today_usd,
                positions=positions,
                asset_id=market.asset_id,
                price_usd=market.price_usd,
                # Marks from recorded observations of each holding's own market,
                # re-checked here against the authoritative instant.
                marks=valuation.by_asset,
                now=now,
                max_snapshot_age_seconds=self.limits.max_snapshot_age_seconds,
                correlation_id=trade_case.correlation_id,
                market=trade_case.market,
            )
            if state.unmarked_assets:
                # SENTINEL would answer `PORTFOLIO_DATA_UNKNOWN`, terminally. A
                # holding this system cannot value is a missing capability, not
                # a judgement about this market.
                return _refused(
                    trade_case,
                    RiskRequestRefusal.PORTFOLIO_MARKS_UNAVAILABLE,
                    assessed.readiness,
                    detail=unvaluable_reason(valuation, state.unmarked_assets),
                )
            if state.conflicting_market is not None:
                # This asset is already held, bought in another market. A fill
                # would merge the two into one position recorded against one of
                # them. Approving an order that could only be filled by doing
                # that would spend the case's one request on an impossibility.
                return _refused(
                    trade_case,
                    RiskRequestRefusal.POSITION_MARKET_CONFLICT,
                    assessed.readiness,
                )

            intent = _trade_intent(assessed, market, trade_case, request_key, now)
            limits = self.limits.model_copy(
                update={"kill_switch": self.limits.kill_switch or bool(account.paused)}
            )
            decision = evaluate(intent, market, state.context, limits, now=now)

            # The existing binding, under the existing safety digest. Neither is
            # redefined: everything else this verdict rested on is bound
            # separately, by `risk_request_digest`.
            safety_digest = trade_case.risk_input_digest
            if safety_digest is None:  # pragma: no cover - READY_FOR_RISK implies one
                raise RiskRequestUnavailable("RISK_INPUT_DIGEST_MISSING")
            binding_id = await self.cases.record_risk_decision_in_session(
                session, row, decision, risk_input_digest=safety_digest
            )
            if decision.outcome is RiskOutcome.PAUSE_SYSTEM:
                # The stop this flow could previously only observe. Set in the
                # same transaction as the rejection it came with, so no window
                # exists where the verdict stands and the pause does not.
                # Nothing here ever clears it.
                account.paused = True

            basis = _basis(
                request_key,
                trade_case,
                assessed,
                limits,
                state,
                intent,
                decision,
                market,
                portfolio_basis(state),
            )
            digest = risk_request_digest(basis)
            request_id = uuid5(NAMESPACE_URL, f"rh-agents:risk-request:{request_key}")
            authorization = classify_decision(decision)
            session.add(
                TradeCaseRiskRequestRow(
                    request_id=request_id,
                    trade_case_id=trade_case_id,
                    request_key=request_key,
                    case_revision=trade_case.revision,
                    risk_input_digest=safety_digest,
                    risk_request_digest=digest,
                    intent_id=intent.id,
                    intent_fingerprint=intent.fingerprint(),
                    requested_notional_usd=assessed.sizing.requested_notional_usd,
                    quantity=assessed.sizing.quantity,
                    risk_decision_id=decision.id,
                    binding_id=binding_id,
                    outcome=decision.outcome.value,
                    authorization=authorization.value,
                    evaluated_at=decision.evaluated_at,
                    recorded_at=now,
                    correlation_id=trade_case.correlation_id,
                    basis=basis,
                )
            )
            return RiskRequestEvaluated(
                request_id=request_id,
                request_key=request_key,
                trade_case_id=trade_case_id,
                case_revision=trade_case.revision,
                outcome=decision.outcome,
                authorization=authorization,
                risk_decision_id=decision.id,
                binding_id=binding_id,
                risk_input_digest=safety_digest,
                risk_request_digest=digest,
                intent_id=intent.id,
                intent_fingerprint=intent.fingerprint(),
                reason_codes=decision.reason_codes,
                evaluated_at=decision.evaluated_at,
                expires_at=decision.expires_at,
                replayed=False,
                blockers=assessed.readiness.blockers,
            )

    async def _already_evaluated(self, trade_case_id: UUID) -> bool:
        async with self.sessions() as session:
            found = await session.scalar(
                select(TradeCaseRiskRequestRow.request_id).where(
                    TradeCaseRiskRequestRow.trade_case_id == trade_case_id
                )
            )
        return found is not None

    async def _value_portfolio(self, feed: OneSnapshot) -> PortfolioValuation:
        """Price every open holding from the market it was acquired in."""
        async with self.sessions() as session:
            positions = [
                read_position(item) for item in (await session.scalars(select(PositionRow))).all()
            ]
        return await PositionValuationReader(
            markets=feed,
            max_age_seconds=self.limits.max_snapshot_age_seconds,
            include_fixtures=self.include_fixtures,
        ).value(positions, self.clock.now())

    async def _assess(
        self,
        feed: OneSnapshot,
        trade_case: TradeCase,
        expected_revision: int | None,
        expected_digest: str | None,
        *,
        paused: bool,
    ) -> CanonicalInputs | RiskRequestRefused:
        """Every precondition, checked before SENTINEL is asked anything."""
        readiness = await RiskDataReader(
            cases=self.cases,
            markets=feed,
            costs=self.costs,
            clock=self.clock,
            pause=self.pause,
            include_fixtures=self.include_fixtures,
        ).readiness(trade_case.id)

        if self.kill_switch:
            return _refused(trade_case, RiskRequestRefusal.KILL_SWITCH_ENGAGED, readiness)
        if self.pause is None:
            # No stop source configured. A service that may progress a case
            # toward risk must be told where the stop lives; not knowing is not
            # the same as being told there is none.
            return _refused(trade_case, RiskRequestRefusal.SYSTEM_STOP_UNREADABLE, readiness)
        if paused:
            return _refused(trade_case, RiskRequestRefusal.SYSTEM_PAUSED, readiness)

        if trade_case.status in TERMINAL_CASE_STATUSES:
            return _refused(trade_case, RiskRequestRefusal.TRADE_CASE_TERMINAL, readiness)
        if trade_case.status is not TradeCaseStatus.READY_FOR_RISK:
            # The evaluator publishes READY_FOR_RISK exactly when no usable
            # authorization covers the case. Asking at any other status would be
            # asking a question the workflow has not reached.
            return _refused(trade_case, RiskRequestRefusal.TRADE_CASE_NOT_READY_FOR_RISK, readiness)
        if expected_revision is not None and trade_case.revision != expected_revision:
            return _refused(trade_case, RiskRequestRefusal.SOURCE_CHANGED_DURING_REQUEST, readiness)
        if expected_digest is not None and trade_case.risk_input_digest != expected_digest:
            return _refused(trade_case, RiskRequestRefusal.SOURCE_CHANGED_DURING_REQUEST, readiness)
        if not readiness.complete:
            return _refused(trade_case, RiskRequestRefusal.RISK_DATA_INCOMPLETE, readiness)

        sizing = await PaperSizingReader(
            cases=self.cases,
            markets=feed,
            requested_notional_usd=self.requested_notional_usd,
            trading_mode=self.trading_mode,
            clock=self.clock,
            include_fixtures=self.include_fixtures,
        ).sizing(trade_case.id)
        if not isinstance(sizing, SizingAssessment):
            return _refused(
                trade_case,
                RiskRequestRefusal.SIZING_INPUT_UNAVAILABLE,
                readiness,
                sizing_reason=sizing.reason,
            )

        snapshot = await feed.latest(trade_case.market.pair_id)
        current = active_evidence(await self.cases.evidence(trade_case.id))
        onchain = current.get(EvidenceType.ONCHAIN)
        anchor = current.get(EvidenceType.LIQUIDITY_EXECUTION)
        if snapshot is None or onchain is None or anchor is None:
            # The completeness check just said these exist. Disagreeing with it
            # is a defect in this service, not a state of the case.
            raise RiskRequestUnavailable("CANONICAL_INPUTS_INCONSISTENT")
        if not isinstance(self.costs, PaperCostAssumptions):  # pragma: no cover - guarded above
            raise RiskRequestUnavailable("CANONICAL_INPUTS_INCONSISTENT")
        return CanonicalInputs(
            readiness=readiness,
            sizing=sizing,
            costs=self.costs,
            snapshot=snapshot,
            onchain=onchain,
            anchor=anchor,
            canonical_evidence={kind.value: item for kind, item in current.items()},
        )


def _refused(
    trade_case: TradeCase,
    reason: RiskRequestRefusal,
    readiness: RiskDataReadiness | None = None,
    *,
    sizing_reason: SizingRefusal | None = None,
    detail: str | None = None,
) -> RiskRequestRefused:
    return RiskRequestRefused(
        reason=reason,
        trade_case_id=trade_case.id,
        trade_case_status=trade_case.status.value,
        data_gaps=() if readiness is None else readiness.gaps,
        sizing_reason=sizing_reason,
        blockers=() if readiness is None else readiness.blockers,
        detail=detail,
    )


def _replay(
    stored: TradeCaseRiskRequestRow, status: TradeCaseStatus, request_key: str
) -> RiskRequestReading:
    """Return the stored verdict, or refuse. Never recompute under one key.

    A case has one canonical trade request. A retry of that request finds the
    same bound inputs and the same result; a *different* key is a second request
    the case is not entitled to, and a changed data situation is not by itself
    permission for one.
    """
    if stored.request_key != request_key:
        return RiskRequestRefused(
            reason=RiskRequestRefusal.RISK_REQUEST_ALREADY_EXISTS,
            trade_case_id=stored.trade_case_id,
            trade_case_status=status.value,
        )
    decision = stored.basis["decision"]
    return RiskRequestEvaluated(
        request_id=stored.request_id,
        request_key=stored.request_key,
        trade_case_id=stored.trade_case_id,
        case_revision=stored.case_revision,
        outcome=RiskOutcome(stored.outcome),
        authorization=RiskAuthorization(stored.authorization),
        risk_decision_id=stored.risk_decision_id,
        binding_id=stored.binding_id,
        risk_input_digest=stored.risk_input_digest,
        risk_request_digest=stored.risk_request_digest,
        intent_id=stored.intent_id,
        intent_fingerprint=stored.intent_fingerprint,
        reason_codes=tuple(decision["reason_codes"]),
        evaluated_at=aware(stored.evaluated_at),
        expires_at=decision["expires_at"],
        replayed=True,
    )


def risk_market(
    *,
    base_asset_id: str,
    price: ReferencePrice,
    base_asset: BaseAssetMetadata,
    snapshot: RecordedMarket,
    onchain: EvidenceEnvelope,
    anchor: EvidenceEnvelope,
    costs: PaperCostAssumptions,
    correlation_id: UUID,
    identity_key: str,
) -> MarketSnapshot:
    """Build SENTINEL's market view from facts that were actually established.

    Every nested observation keeps the instant *its own source* recorded, never
    the moment this ran. SENTINEL checks each of those ages independently, so an
    assembly time written here would rejuvenate every source at once and hand it
    a freshness none of them had.

    Identifiers are derived from `identity_key` rather than generated, so the
    same inputs always produce the same market fingerprint and a stored basis
    can be recomputed and checked.
    """
    payload = onchain.payload
    execution = anchor.payload
    if not isinstance(payload, OnchainPayload) or not isinstance(
        execution, LiquidityExecutionPayload
    ):  # pragma: no cover - guarded by the completeness check
        raise RiskRequestUnavailable("CANONICAL_INPUTS_INCONSISTENT")
    holders = None if payload.intelligence is None else payload.intelligence.holders
    liquidity = snapshot.liquidity
    if (
        holders is None
        or holders.top_ten_fraction is None
        or holders.holder_count is None
        or liquidity.status is not Availability.AVAILABLE
        or liquidity.value_usd is None
    ):  # pragma: no cover - guarded by the completeness check
        raise RiskRequestUnavailable("CANONICAL_INPUTS_INCONSISTENT")

    def identity(label: str) -> UUID:
        return uuid5(NAMESPACE_URL, f"rh-agents:risk-market:{identity_key}:{label}")

    # The accounting boundary. Recorded market facts carry the market layer's
    # precision; SENTINEL's snapshot is ledger-typed `Numeric(38, 18)`. The
    # conversion happens here, explicitly and deterministically, rather than as
    # a validation error somewhere inside the model constructor.
    price_usd = quantize(price.usd_per_base_unit)
    if price_usd <= 0:
        # A real, positive price the ledger cannot express. Never passed on as
        # zero and never raised to the smallest step: there is no honest ledger
        # price for it, so there is no request.
        raise RiskRequestUnavailable("REFERENCE_PRICE_OUTSIDE_ACCOUNTING_PRECISION")
    # Liquidity is floored, never rounded half-even: rounding up could lift a
    # reading just below `min_liquidity_usd` over it. A floor can only make the
    # market look thinner, and a sub-precision reading becomes zero, which the
    # engine rejects as insufficient liquidity.
    liquidity_usd = quantize_down(liquidity.value_usd)

    price_at = price.observed_at
    return MarketSnapshot(
        id=identity("market"),
        created_at=price_at,
        updated_at=price_at,
        source=price.provider,
        correlation_id=correlation_id,
        asset_id=base_asset_id,
        observed_at=price_at,
        price_usd=price_usd,
        token=TokenSnapshot(
            id=identity("token"),
            created_at=base_asset.source_observed_at,
            updated_at=base_asset.source_observed_at,
            source=base_asset.source_provider,
            correlation_id=correlation_id,
            asset_id=base_asset_id,
            symbol=base_asset.symbol,
            decimals=base_asset.decimals,
            # Contract integrity is exactly the token-level safety question:
            # code present, supply known, no unreviewed proxy admin. Whether a
            # route exists is a different question and travels on `routing`.
            tradable=SAFETY_STATUS[payload.contract_integrity],
        ),
        liquidity=LiquiditySnapshot(
            id=identity("liquidity"),
            created_at=liquidity.observed_at,
            updated_at=liquidity.observed_at,
            source=liquidity.provider,
            correlation_id=correlation_id,
            asset_id=base_asset_id,
            liquidity_usd=liquidity_usd,
            # ANCHOR established an executable route at a tested size, which is
            # what this field asks. It is not a claim about the price.
            routing=SafetyStatus.PASS,
            # A configured simulation assumption, never a measured fill cost and
            # never ANCHOR's execution deviation.
            estimated_slippage_bps=costs.slippage_bps,
        ),
        holders=HolderSnapshot(
            id=identity("holders"),
            created_at=holders.observed_at,
            updated_at=holders.observed_at,
            source=holders.source,
            correlation_id=correlation_id,
            asset_id=base_asset_id,
            holder_count=holders.holder_count,
            # Top ten over total supply, unadjusted — the measure this field
            # means and the one the limit is written against.
            top_ten_fraction=holders.top_ten_fraction,
            concentration_check=SAFETY_STATUS[payload.holder_integrity],
        ),
        # The configured proportional fee on one side's executed notional.
        fee_bps=costs.fee_bps,
    )


# The suffix a staleness detail carries, and the only way to read a source name
# back out of one. Written once so the check that produces the label and the
# contract that acts on it cannot drift apart.
STALE_SUFFIX = "_OLDER_THAN_RISK_LIMIT"


def stale_source(detail: str | None) -> str | None:
    """The source a staleness detail names, if it names one.

    A refusal detail is a code, not prose, so this is a lookup rather than
    parsing. Anything that is not a staleness label — an observation from the
    future, for instance — names no refreshable source and returns nothing.
    """
    if detail is None or not detail.endswith(STALE_SUFFIX):
        return None
    return detail[: -len(STALE_SUFFIX)]


def too_old_for(market: MarketSnapshot, now: datetime, limits: RiskLimits) -> str | None:
    """Apply SENTINEL's own configured tolerance before SENTINEL is asked.

    The same bound it would apply, to the same instants, so a stale reading is
    reported as a stale reading instead of coming back as a terminal rejection
    of the market. Nothing is loosened to make a source fit.
    """
    for label, instant in (
        ("MARKET", market.observed_at),
        ("TOKEN", market.token.created_at),
        ("LIQUIDITY", market.liquidity.created_at),
        ("HOLDERS", market.holders.created_at),
    ):
        age = (now - instant).total_seconds()
        if age < 0:
            return f"{label}_OBSERVED_IN_THE_FUTURE"
        if age > limits.max_snapshot_age_seconds:
            return f"{label}{STALE_SUFFIX}"
    return None


def _trade_intent(
    inputs: CanonicalInputs,
    market: MarketSnapshot,
    trade_case: TradeCase,
    request_key: str,
    now: datetime,
) -> TradeIntent:
    """The first and only trade intent this system constructs.

    Its identity comes from the request key, so a retry finds the same object
    rather than minting a new one — the correction Phase 2M-A recorded, where an
    identity derived from live inputs could not survive the one situation it
    existed for.

    ``timing`` describes when this *request* was formed, which is now. It is not
    a claim about the age of the facts underneath: each of those carries the
    instant its own source recorded, and each is checked separately above.
    """
    sizing = inputs.sizing
    if sizing.side is not Side.BUY:  # pragma: no cover - the policy is long-only
        raise RiskRequestUnavailable("CANONICAL_INPUTS_INCONSISTENT")
    return TradeIntent(
        id=uuid5(NAMESPACE_URL, f"rh-agents:risk-request-intent:{request_key}"),
        created_at=now,
        updated_at=now,
        source="RISK_REQUEST",
        correlation_id=trade_case.correlation_id,
        asset_id=sizing.base_asset_id,
        side=Side.BUY,
        quantity=sizing.quantity,
        signal_price=market.price_usd,
        # The tolerance being asked about is exactly the move the simulation
        # assumes. SENTINEL still narrows it to its own configured ceiling.
        max_slippage_bps=inputs.costs.slippage_bps,
        mode=TradingMode.PAPER,
        timing=ExecutionTiming(detected_at=now, decision_at=now),
    )


def _basis(
    request_key: str,
    trade_case: TradeCase,
    inputs: CanonicalInputs,
    limits: RiskLimits,
    state: PortfolioState,
    intent: TradeIntent,
    decision: Any,
    market: MarketSnapshot,
    portfolio: dict[str, Any],
) -> dict[str, Any]:
    """The whole decision basis, persisted beside the verdict.

    Recorded rather than referenced, because several of these are values the
    sources will not keep: a portfolio moves with every fill, and a limits set
    is configuration that can be edited. A binding that named them instead of
    holding them could not be checked a month later.
    """
    return {
        "request_key": request_key,
        "trade_case_id": str(trade_case.id),
        "case_revision": trade_case.revision,
        "workflow_version": trade_case.workflow_version,
        "safety_risk_input_digest": trade_case.risk_input_digest,
        "sizing": inputs.sizing.model_dump(mode="json"),
        "readiness": inputs.readiness.model_dump(mode="json"),
        "cost_assumptions": inputs.costs.model_dump(mode="json"),
        "risk_limits": limits.model_dump(mode="json"),
        "portfolio": portfolio,
        # The typed context `evaluate` was actually given. `portfolio` carries
        # the holdings it was computed from, so this figure can be recomputed
        # rather than taken on trust.
        "risk_context": state.context.model_dump(mode="json"),
        # The typed snapshot `evaluate` was actually given, whole. The
        # completeness reading records provenance rather than values, so without
        # this the only way to learn what SENTINEL was shown would be to re-read
        # sources that have since moved — and the decision's own
        # `market_fingerprint` could never be checked again.
        "market_snapshot": market.model_dump(mode="json"),
        # The canonical envelopes the values came out of, by identity and
        # fingerprint, so each figure can be traced back to the exact row that
        # carried it rather than to a role that has produced many.
        "evidence": {
            kind: {
                "evidence_id": str(item.evidence_id),
                "producer_role": item.producer_role.value,
                "submission_fingerprint": item.submission_fingerprint,
                "observed_at": item.observed_at.isoformat(),
                "valid_until": item.valid_until.isoformat(),
            }
            for kind, item in sorted(inputs.canonical_evidence.items(), key=lambda pair: pair[0])
        },
        "intent": intent.model_dump(mode="json"),
        "intent_fingerprint": intent.fingerprint(),
        "decision": decision.model_dump(mode="json"),
    }
