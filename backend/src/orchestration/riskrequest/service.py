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
from typing import Any, NamedTuple
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.agents.atlas.v4.protocol import V4_DEPLOYMENTS
from src.core.clock import Clock, SystemClock
from src.core.models import (
    LP_CONTROL_V4_POOL_CENSUS,
    ExecutionTiming,
    HolderSnapshot,
    LiquiditySnapshot,
    LpControlSnapshot,
    LpControlStatus,
    MarketSnapshot,
    PoolLocatorCode,
    RiskLimits,
    RiskOutcome,
    SafetyStatus,
    Side,
    TokenSnapshot,
    TradeIntent,
    TradingMode,
)
from src.core.numbers import LEDGER_UNIT, fits_ledger, quantize_down, quantize_up
from src.data.repository import aware, read_position
from src.data.tables import AccountRow, PositionRow, TradeCaseRiskRequestRow
from src.ledger.portfolio import (
    PortfolioState,
    portfolio_basis,
    portfolio_state,
    roll_loss_day,
)
from src.markets.models import Availability, PoolLocatorKind
from src.markets.models import MarketSnapshot as RecordedMarket
from src.markets.scope import MarketScope, latest_in
from src.orchestration.commander.context import SystemPausePort
from src.orchestration.costs.models import PaperCostAssumptions, PaperCostReading
from src.orchestration.entryhalt.models import entry_halt
from src.orchestration.entryhalt.service import halt_on_daily_loss
from src.orchestration.jobruns.models import ExitLivenessGate, judge_exit_liveness
from src.orchestration.jobruns.service import exit_evidence, policy_class
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
from src.orchestration.strategy.early import (
    EARLY_ENTRY_V1,
    cap_refusal,
    capacity_refusal,
    early_ledger,
    is_early,
    limits_for,
)
from src.orchestration.valuation.models import PortfolioValuation, unvaluable_reason
from src.orchestration.valuation.service import PositionValuationReader, held_market_identities
from src.orchestration.workflow.engine import active_evidence
from src.orchestration.workflow.models import (
    TERMINAL_CASE_STATUSES,
    EvidenceEnvelope,
    EvidenceType,
    HolderDistributionFacts,
    LiquidityExecutionPayload,
    OnchainIntelligence,
    OnchainPayload,
    PoolControlPool,
    PoolControlSummary,
    TradeCase,
    TradeCaseStatus,
    WorkflowFailure,
)
from src.orchestration.workflow.service import TradeCaseService, case_from_row
from src.risk.authorization import RiskAuthorization, classify_decision
from src.risk.engine import evaluate

# An ATLAS domain verdict as SENTINEL reads it. For an entry an unestablished
# domain is a completeness gap the readiness check refuses on first; an exit's
# fresh read has no such check and can carry UNKNOWN beside measured holder
# figures. UNKNOWN therefore stays unknown -- every BUY and every sale fails
# closed on it -- and is never a missing key.
SAFETY_STATUS = {
    "PASS": SafetyStatus.PASS,
    "FAIL": SafetyStatus.FAIL,
    "UNKNOWN": SafetyStatus.UNKNOWN,
}


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
        self._scoped: dict[MarketScope, RecordedMarket | None] = {}

    async def latest(
        self, identity: str, *, include_fixtures: bool = False
    ) -> RecordedMarket | None:
        if identity not in self._seen:
            self._seen[identity] = await self._markets.latest(
                identity, include_fixtures=include_fixtures or self._include_fixtures
            )
        return self._seen[identity]

    async def latest_in(
        self, scope: MarketScope, *, include_fixtures: bool = False
    ) -> RecordedMarket | None:
        """The scoped market's own reading, read once per scope like `latest`.

        The mark and the sale of one holding ask with the same scope — the
        case's full identity — so both describe the same observation.
        """
        if scope not in self._scoped:
            self._scoped[scope] = await latest_in(
                self._markets, scope, include_fixtures=include_fixtures or self._include_fixtures
            )
        return self._scoped[scope]


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
    # Split operation only: no request is asked unless the exit job's latest
    # evidence covers the BUY it could lead to. Absent, never consulted.
    exit_liveness: ExitLivenessGate | None = None
    # Early-only operation: only an early case may ask. Off, never consulted.
    early_only: bool = False

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
            # The limits this case is judged against, by its own strategy. A
            # normal case gets exactly `self.limits`; an early one gets the same
            # object with only the minimum liquidity changed.
            case_limits = limits_for(trade_case.strategy_policy_id, self.limits)

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
                # The entry-only halt, from the same locked row.
                entry_halted=entry_halt(account),
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
            # The early strategy's book, read before the clock like every other
            # input; the day boundary is applied at the decision instant below.
            ledger = (
                await early_ledger(session) if is_early(trade_case.strategy_policy_id) else None
            )
            # The exit job's latest evidence, read under the account lock like
            # every other input and judged at the decision instant below.
            evidence = await exit_evidence(session) if self.exit_liveness is not None else None

            # The last clock read, after the last input read. Everything from
            # here to the verdict is synchronous, so one instant governs
            # eligibility, the validity of the basis, every source age, the UTC
            # loss day and SENTINEL itself. An instant taken earlier describes
            # when the loading began, and a case can lapse while it runs.
            now = self.clock.now()
            roll_loss_day(account, now)
            if evidence is not None and self.exit_liveness is not None:
                stale = judge_exit_liveness(
                    evidence,
                    at=now,
                    gate=self.exit_liveness,
                    policy=policy_class(trade_case.strategy_policy_id),
                )
                if stale is not None:
                    return _refused(
                        trade_case,
                        RiskRequestRefusal.EXIT_JOB_STALE,
                        assessed.readiness,
                        detail=stale,
                    )

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

            if is_early(trade_case.strategy_policy_id):
                # The strategy's own conditions, checked before SENTINEL is
                # asked anything and under the account lock that orders fills:
                # the fixed notional must be proven executable, and one more
                # early position must fit inside every early cap.
                execution = getattr(assessed.anchor.payload, "execution", None)
                shortfall = capacity_refusal(
                    None if execution is None else execution.largest_tested_acceptable_notional_usd
                )
                if shortfall is not None:
                    return _refused(
                        trade_case,
                        RiskRequestRefusal.EARLY_EXECUTABLE_CAPACITY_INSUFFICIENT,
                        assessed.readiness,
                        detail=shortfall,
                    )
                capped = (
                    cap_refusal(ledger.at(now)) if ledger is not None else "EARLY_LEDGER_UNREAD"
                )
                if capped is not None:
                    return _refused(
                        trade_case,
                        RiskRequestRefusal.EARLY_STRATEGY_CAP_REACHED,
                        assessed.readiness,
                        detail=capped,
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
                side=Side.BUY,
            )
            stale = too_old_for(market, now, case_limits)
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
            if state.accounting_issues:
                # Every mark is known; the figure SENTINEL would judge is not
                # representable. Asked anyway, it would reject on unknown
                # accounting and spend the case's one request on a capability gap.
                return _refused(
                    trade_case,
                    RiskRequestRefusal.PORTFOLIO_ACCOUNTING_UNREPRESENTABLE,
                    assessed.readiness,
                    detail=state.accounting_issues[0].value,
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
            limits = case_limits.model_copy(
                update={"kill_switch": case_limits.kill_switch or bool(account.paused)}
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
            # A BUY refused at the daily loss limit halts every further entry,
            # in this transaction, beside the verdict that caused it.
            await halt_on_daily_loss(session, account, decision, limits=case_limits, at=now)

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
            identities = await held_market_identities(session, positions)
        return await PositionValuationReader(
            markets=feed,
            max_age_seconds=self.limits.max_snapshot_age_seconds,
            include_fixtures=self.include_fixtures,
            identities=identities,
        ).value(positions, self.clock.now())

    async def _assess(
        self,
        feed: OneSnapshot,
        trade_case: TradeCase,
        expected_revision: int | None,
        expected_digest: str | None,
        *,
        paused: bool,
        entry_halted: str | None = None,
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
        if entry_halted is not None:
            return _refused(
                trade_case, RiskRequestRefusal.ENTRY_HALTED, readiness, detail=entry_halted
            )
        if self.early_only and not is_early(trade_case.strategy_policy_id):
            return _refused(trade_case, RiskRequestRefusal.NORMAL_ENTRY_DISABLED, readiness)

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
            # An early case asks for the strategy's fixed notional, never the
            # operator's configured size; a normal case is unchanged.
            requested_notional_usd=(
                EARLY_ENTRY_V1.notional_usd
                if is_early(trade_case.strategy_policy_id)
                else self.requested_notional_usd
            ),
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
    side: Side,
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
    return market_view(
        base_asset_id=base_asset_id,
        price=price,
        base_asset=base_asset,
        snapshot=snapshot,
        onchain=payload,
        holders=holders,
        # ANCHOR established an executable route at a tested size, which is
        # what this field asks. It is not a claim about the price.
        routing=SafetyStatus.PASS,
        costs=costs,
        correlation_id=correlation_id,
        identity_key=identity_key,
        side=side,
    )


def entry_concentration(
    onchain: OnchainPayload, holders: HolderDistributionFacts
) -> Decimal | None:
    """The top-ten fraction an entry is judged on.

    The raw figure, unless ATLAS recorded that this token's concentration must
    be read through V4 pool control -- then the economic figure, or, where the
    control of some position supply is unresolved, the floor the economic
    figure is known to reach, or nothing. The raw holder record itself stays
    unchanged in the evidence for audit.
    """
    control = None if onchain.intelligence is None else onchain.intelligence.pool_control
    if control is None or not control.required:
        return holders.top_ten_fraction
    if control.economic_concentration is not None:
        return control.economic_concentration
    return control.economic_concentration_floor


def entry_concentration_established(onchain: OnchainPayload) -> bool:
    """Whether the figure an entry is judged on is the whole measure.

    A floor is not: it can prove a limit exceeded, never one respected. So an
    entry judged on one has its holder check unknown, whatever else says PASS.
    """
    control = None if onchain.intelligence is None else onchain.intelligence.pool_control
    return control is None or not control.required or control.economic_concentration is not None


# A V4 pool without a hook. Any other hook is code nobody here has verified,
# and a hook can hold or move liquidity, so its pool's control is unknown.
NO_HOOK = "0x" + "0" * 40


class LpControlFinding(NamedTuple):
    """What the market view records as a BUY's LP-control evidence."""

    status: LpControlStatus
    detail: str
    pool_locator_kind: PoolLocatorCode | None = None
    pool_id: str | None = None
    census_block: int | None = None
    # The census block's chain time, with a PASS: the age SENTINEL judges.
    census_observed_at: datetime | None = None


def lp_control_view(*, snapshot: RecordedMarket, onchain: OnchainPayload) -> LpControlFinding:
    """Whether the traded pool's liquidity control is established, and on what.

    The only evidence there is today is ATLAS's V4 pool census, so only a
    32-byte market pool can pass, and only when the census is bound to the
    verified deployment of the market's chain at the ATLAS block
    (`census_basis_gap`), is complete without any tolerance
    (`unresolved_custody`), lists the market's own pool in that PoolManager,
    and that pool has no hook. A 20-byte pool (V2/V3 and alike) has no LP-control source at all:
    its LP tokens or position NFTs are unread, so it is UNSUPPORTED. Nothing
    here reads a provider.
    """
    locator = snapshot.pair.pool_locator
    if locator is None:
        return LpControlFinding(LpControlStatus.UNKNOWN, "POOL_LOCATOR_UNKNOWN")
    pool_id = locator.value
    kind: PoolLocatorCode = (
        "CONTRACT_ADDRESS"
        if locator.kind is PoolLocatorKind.CONTRACT_ADDRESS
        else "BYTES32_POOL_ID"
    )

    def finding(status: LpControlStatus, detail: str) -> LpControlFinding:
        return LpControlFinding(status, detail, kind, pool_id)

    if locator.kind is PoolLocatorKind.CONTRACT_ADDRESS:
        return finding(LpControlStatus.UNSUPPORTED, "LP_CONTROL_SOURCE_ABSENT")
    if locator.kind is not PoolLocatorKind.BYTES32_POOL_ID:  # pragma: no cover - closed enum
        return finding(LpControlStatus.UNKNOWN, "POOL_LOCATOR_UNKNOWN")
    intelligence = onchain.intelligence
    control = None if intelligence is None else intelligence.pool_control
    if intelligence is None or control is None or not control.required:
        return finding(LpControlStatus.UNKNOWN, "V4_POOL_CENSUS_ABSENT")
    if control.economic_concentration is None:
        return finding(LpControlStatus.UNKNOWN, control.gap or "V4_POOL_CENSUS_UNAVAILABLE")
    unbound = census_basis_gap(
        chain=snapshot.pair.chain, intelligence=intelligence, control=control
    )
    if unbound is not None:
        return finding(LpControlStatus.UNKNOWN, unbound)
    manager = locator.pool_manager_address
    if manager is not None and manager != control.pool_manager:
        # The market says it trades on a PoolManager other than the verified
        # one the census read: the same pool id there is another pool.
        return finding(LpControlStatus.UNKNOWN, "V4_MARKET_POOL_MANAGER_MISMATCH")
    pool = next((item for item in control.pools if item.pool_id == pool_id), None)
    if pool is None or not pool.initialized:
        return finding(LpControlStatus.UNKNOWN, "V4_MARKET_POOL_NOT_IN_CENSUS")
    token = snapshot.pair.base.asset_id.rsplit(":", 1)[-1].lower()
    if token not in (pool.currency0, pool.currency1):
        # Issue #98: checked here, not left to the producer. The evidence is
        # about the market's own token, or it is not evidence for this market.
        return finding(LpControlStatus.UNKNOWN, "V4_MARKET_POOL_TOKEN_MISMATCH")
    if pool.hook != NO_HOOK:
        if pool.hook_owner_is_creator is True:
            # Established: the creator owns the code every liquidity change in
            # the market's pool runs through.
            return finding(LpControlStatus.BLOCKED, "V4_HOOK_CREATOR_CONTROLLED")
        return finding(LpControlStatus.UNKNOWN, "V4_HOOK_UNVERIFIED")
    if pool.positions_creator_withdrawable:
        # Established: the creator can take the market's liquidity now.
        return finding(LpControlStatus.BLOCKED, "V4_CREATOR_CAN_WITHDRAW")
    unresolved = unresolved_custody(control)
    if unresolved is not None:
        return finding(LpControlStatus.UNKNOWN, unresolved)
    restricted = withdrawal_unrestricted(pool)
    if restricted is not None:
        return finding(LpControlStatus.UNKNOWN, restricted)
    return LpControlFinding(
        LpControlStatus.PASS,
        LP_CONTROL_V4_POOL_CENSUS,
        kind,
        pool_id,
        control.snapshot_block,
        control.snapshot_block_timestamp,
    )


def census_basis_gap(
    *, chain: str, intelligence: OnchainIntelligence, control: PoolControlSummary
) -> str | None:
    """Why the census is not evidence for this market's chain and ATLAS read, if not.

    A census proves something only about the PoolManager and block it was read
    at. That manager must be the verified V4 deployment of the market's chain,
    and every PositionManager it named one of that deployment's; its chain must
    be the ATLAS evidence's; and its block must be the block that evidence was
    pinned to, so a census from another read cannot be carried beside it. And
    its block's chain time must be known: that, not the holder observation
    beside it, is how old the census is.
    """
    if control.pool_manager is None or control.snapshot_block is None:
        return "V4_POOL_CENSUS_BASIS_INCOMPLETE"
    if control.chain_id is None or control.chain_id != intelligence.chain_id:
        return "V4_CENSUS_CHAIN_MISMATCH"
    deployment = V4_DEPLOYMENTS.get((chain, control.chain_id))
    if deployment is None:
        return "V4_DEPLOYMENT_UNKNOWN"
    if control.pool_manager != deployment.pool_manager:
        return "V4_CENSUS_POOL_MANAGER_UNVERIFIED"
    if not set(control.position_managers) <= set(deployment.position_managers):
        return "V4_CENSUS_POSITION_MANAGER_UNVERIFIED"
    if control.snapshot_block != intelligence.block_number:
        return "V4_CENSUS_BLOCK_MISMATCH"
    if control.snapshot_block_timestamp is None:
        return "V4_CENSUS_BLOCK_TIME_UNKNOWN"
    return None


# The position control states that settle who can move a position's principal:
# a named controller, or proof that nobody can. Anything else is unresolved.
RESOLVED_CONTROL_STATES = frozenset({"DIRECT_CONTROL", "RELEASABLE", "PERMANENTLY_LOCKED"})


# Reconciliation gaps that say the census contradicts itself, not that a read
# was merely unavailable.
INCONSISTENT_RECONCILIATION = frozenset({"V4_POOL_BALANCE_DEFICIT", "V4_CLAIMS_INCONSISTENT"})


def unresolved_custody(control: PoolControlSummary) -> str | None:
    """Why some liquidity in the census has no established controller, if any does.

    The economic concentration tolerates a small untraced remainder (fees,
    round-down), because it ranks holders of token supply. LP control asks who
    can withdraw a pool's liquidity, and a token-supply tolerance proves nothing
    about that: a position with an unknown owner can hold almost no token and
    most of the pool's ETH. So there is no tolerance here. Every position is
    attributed, nothing in the PoolManager is unexplained (`unexplained_remainder`),
    the concentration is exact, and every position's control is resolved -- or
    the control is unknown.
    """
    if control.positions_unattributed != 0:
        return "V4_POSITION_OWNER_UNKNOWN"
    if control.reconciliation_gap in INCONSISTENT_RECONCILIATION:
        # The reconciliation read liabilities the balance cannot hold: the
        # census misread something, whatever the remainder looks like.
        return "V4_POOL_BALANCE_INCONSISTENT"
    if control.unattributed_raw != "0":
        unexplained = unexplained_remainder(control)
        if unexplained is not None:
            return unexplained
    if control.basis != "EXACT":
        return "V4_CONCENTRATION_NOT_EXACT"
    states = control.position_control_states
    if not states and control.positions_total:
        # None: recorded before control facts existed. Empty: none was counted.
        # Either way no position's control was read.
        return "V4_POSITION_CONTROL_UNRESOLVED"
    if any(state not in RESOLVED_CONTROL_STATES for state in states or {}) or any(
        raw not in (None, "0") for raw in (control.timelocked_raw, control.unknown_custody_raw)
    ):
        return "V4_POSITION_CONTROL_UNRESOLVED"
    return None


def withdrawal_unrestricted(pool: PoolControlPool) -> str | None:
    """Why some of the market pool's liquidity could be withdrawn now, if any could.

    Issue #97. Known control is not restricted withdrawal: an account holding a
    position can remove it in the next block, and nobody here can tell a third
    party from the creator's other wallet. So a purchase passes only when every
    position in the market's own pool is verified permanently locked. (One the
    verified creator can withdraw is BLOCKED before this is asked.) Positions in
    the token's other pools cannot drain this one; their custody is judged by
    `unresolved_custody`.
    """
    counts = (
        pool.positions_total,
        pool.positions_withdrawal_restricted,
        pool.positions_withdrawable,
    )
    if any(value is None for value in counts):
        # Recorded before withdrawal was counted.
        return "V4_WITHDRAWAL_RESTRICTION_UNKNOWN"
    if pool.positions_withdrawable:
        return "V4_LIQUIDITY_WITHDRAWABLE"
    if not pool.positions_total or pool.positions_withdrawal_restricted != pool.positions_total:
        # No position to lock anything, or one whose withdrawal is unresolved.
        return "V4_WITHDRAWAL_RESTRICTION_UNKNOWN"
    return None


def credit_adds_up(control: PoolControlSummary) -> bool:
    """Whether a COMPLETE remainder credit is consistent with the rest of the evidence.

    Re-checked here, not taken from the status alone: nothing uncredited, the
    credited and unowned parts make up the remainder exactly, and an unowned
    part is never more than the surplus and exists only over a discovery that
    covers the PoolManager's whole history.
    """
    figures = (
        control.unattributed_raw,
        control.remainder_credited_raw,
        control.remainder_unowned_raw,
        control.remainder_uncredited_raw,
        control.surplus_raw,
    )
    if any(value is None for value in figures):
        return False
    unattributed, credited, unowned, uncredited, surplus = (
        int(value) for value in figures if value is not None
    )
    return (
        uncredited == 0
        and credited + unowned == unattributed
        and unowned <= surplus
        and (unowned == 0 or control.pool_discovery_basis == "MANAGER_HISTORY")
    )


def unexplained_remainder(control: PoolControlSummary) -> str | None:
    """Why a PoolManager remainder beyond the positions' principal blocks a PASS, if it does.

    Issue #96: the census reconciles the balance against every liability of
    the positions it found -- uncollected fees, protocol fees, ERC-6909 claims
    -- and whatever lies beyond. Issue #103: the economic split credits each
    part to who can take it. A remainder passes only when that credit is
    COMPLETE: fees to the collector of a withdrawable position, protocol fees
    to the protocol, claims to the holders their transfers name, and a surplus
    to nobody only over a discovery covering the PoolManager's whole history
    (#94; v4-core lets a pool be initialized before its token exists).
    Otherwise the reason is exact. No tolerance, no estimate. Called only with
    every position attributed.
    """
    if control.reconciliation_status != "COMPLETE":
        return "V4_POOL_BALANCE_UNATTRIBUTED"
    parts = (
        control.uncollected_fees_raw,
        control.protocol_fees_raw,
        control.claims_raw,
        control.surplus_raw,
    )
    if control.unattributed_raw is None or any(part is None for part in parts):
        return "V4_POOL_BALANCE_UNATTRIBUTED"
    if int(control.unattributed_raw) != sum(int(part) for part in parts if part is not None):
        # The reconciliation and the economic split describe different states.
        return "V4_POOL_BALANCE_UNATTRIBUTED"
    if control.remainder_credit_status == "COMPLETE" and credit_adds_up(control):
        # Issue #103: every part credited to who can take it -- fees to their
        # collector, protocol fees to the protocol, claims to their holders --
        # or provably nobody's, and the economic figure ranks each with its
        # holder. The concentration and every position's control are still
        # judged below.
        return None
    if control.claims_raw != "0":
        return "V4_POOL_CLAIMS_UNATTRIBUTED"
    if control.surplus_raw != "0" and control.pool_discovery_basis != "MANAGER_HISTORY":
        # Discovery from the token's creation block can miss a pool initialized
        # before the token existed; whatever such a pool holds sits here.
        return "V4_POOL_SURPLUS_UNEXPLAINED"
    if control.uncollected_fees_raw != "0" or control.protocol_fees_raw != "0":
        return "V4_POOL_FEES_NOT_CREDITED"
    # Only a surplus is left, over a discovery covering the PoolManager's whole
    # history, but no credit was recorded for it (evidence before #103).
    return "V4_POOL_SURPLUS_NOT_CREDITED"


def market_view(
    *,
    base_asset_id: str,
    price: ReferencePrice,
    base_asset: BaseAssetMetadata,
    snapshot: RecordedMarket,
    onchain: OnchainPayload,
    holders: HolderDistributionFacts,
    routing: SafetyStatus,
    costs: PaperCostAssumptions,
    correlation_id: UUID,
    identity_key: str,
    side: Side,
) -> MarketSnapshot:
    """SENTINEL's market view from established facts, whoever established them.

    `risk_market` supplies the entry's evidence; a PAPER exit supplies its own
    fresh read. Either way each nested observation keeps its source's instant.
    An unavailable liquidity reading is carried as unknown, for SENTINEL to
    refuse, never replaced.
    """
    payload = onchain
    liquidity = snapshot.liquidity
    available = liquidity.status is Availability.AVAILABLE and liquidity.value_usd is not None

    def identity(label: str) -> UUID:
        return uuid5(NAMESPACE_URL, f"rh-agents:risk-market:{identity_key}:{label}")

    # The accounting boundary. Recorded market facts carry the market layer's
    # precision; SENTINEL's snapshot is ledger-typed `Numeric(38, 18)`. The
    # conversion happens here, explicitly and in the conservative direction for
    # each field, rather than as a validation error inside a constructor.
    #
    # Price: rounded against the order. A buy is sized by dividing by the exact
    # recorded price and flooring the quantity, and SENTINEL then checks
    # `quantity * price_usd`; a ledger price below the recorded one would
    # understate that notional, which for a cheap enough token is material. So
    # a buy is priced by a ceiling and a sell (an exit) by a floor: each can
    # only make the order look more expensive or less rewarding, and never by
    # as much as one ledger unit per token.
    recorded_price = price.usd_per_base_unit
    if recorded_price < LEDGER_UNIT or not fits_ledger(recorded_price):
        # Below the smallest ledger unit, raising it to that unit would change
        # the price by up to its whole value; above the ledger's range there is
        # nothing to store. Neither is zeroed, raised or capped.
        raise RiskRequestUnavailable("REFERENCE_PRICE_OUTSIDE_ACCOUNTING_PRECISION")
    price_usd = quantize_up(recorded_price) if side is Side.BUY else quantize_down(recorded_price)
    if not fits_ledger(price_usd):
        # Rounding up carried into a twenty-first integer digit.
        raise RiskRequestUnavailable("REFERENCE_PRICE_OUTSIDE_ACCOUNTING_PRECISION")
    # Liquidity: floored, never rounded half-even or up, so a reading just below
    # `min_liquidity_usd` cannot be lifted over it. A sub-precision reading
    # becomes zero, which the engine rejects as insufficient liquidity. A
    # reading too large for the ledger is refused rather than capped.
    liquidity_usd: Decimal | None = None
    if available and liquidity.value_usd is not None:
        if not fits_ledger(liquidity.value_usd):
            raise RiskRequestUnavailable("LIQUIDITY_OUTSIDE_ACCOUNTING_PRECISION")
        liquidity_usd = quantize_down(liquidity.value_usd)

    lp_control = None
    if side is Side.BUY:
        # Issue #92: only a purchase is judged on it. A sale's snapshot keeps
        # the exact shape it always had.
        found = lp_control_view(snapshot=snapshot, onchain=payload)
        # A PASS is as old as its census block, which ATLAS lets lie minutes
        # from the holder observation; dating it by that observation would
        # hand the census a freshness it never had. Without a PASS there is no
        # census to date, and the finding stands beside the holder facts.
        lp_control_at = found.census_observed_at or holders.observed_at
        lp_control = LpControlSnapshot(
            id=identity("lp-control"),
            created_at=lp_control_at,
            updated_at=lp_control_at,
            source=holders.source,
            correlation_id=correlation_id,
            asset_id=base_asset_id,
            status=found.status,
            pool_locator_kind=found.pool_locator_kind,
            pool_id=found.pool_id,
            basis=LP_CONTROL_V4_POOL_CENSUS if found.status is LpControlStatus.PASS else None,
            census_block=found.census_block,
            detail=found.detail,
        )

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
            routing=routing,
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
            # Top ten over total supply — the measure the limit is written
            # against. For a purchase of a token held in V4 pools it is the
            # economic figure, which counts supply in liquidity positions with
            # the party that controls it; unestablished, it is unknown and the
            # engine refuses. A sale is not judged on concentration, so it
            # keeps the raw figure and is never trapped by a missing census.
            top_ten_fraction=(
                entry_concentration(payload, holders)
                if side is Side.BUY
                else holders.top_ten_fraction
            ),
            concentration_check=(
                SAFETY_STATUS[payload.holder_integrity]
                if side is not Side.BUY or entry_concentration_established(payload)
                else SafetyStatus.UNKNOWN
            ),
        ),
        # The configured proportional fee on one side's executed notional.
        fee_bps=costs.fee_bps,
        lp_control=lp_control,
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
    instants = [
        ("MARKET", market.observed_at),
        ("TOKEN", market.token.created_at),
        ("LIQUIDITY", market.liquidity.created_at),
        ("HOLDERS", market.holders.created_at),
    ]
    if market.lp_control is not None:
        # Only a purchase carries it, and SENTINEL then judges its age too.
        instants.append(("LP_CONTROL", market.lp_control.created_at))
    for label, instant in instants:
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
