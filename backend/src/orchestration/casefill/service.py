"""The one narrow server-side call that may fill a stored risk request.

Everything happens in a single transaction, in the order the rest of this system
already established: paper account, then trade case. The fill, the ledger
postings and the case's own record of them commit together or not at all — a
crash between them would leave a position nothing points at, or a case that
believes it traded when no money moved.

The caller names the stored request and, optionally, the bindings it expects. It
supplies no quantity, price, `RiskInput`, limit or portfolio value. Every one of
those is either taken from the stored request or assembled here from the source
that owns it.
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
    ExecutionResult,
    RiskDecision,
    RiskLimits,
    TradeIntent,
    TradingMode,
)
from src.data.repository import aware
from src.data.tables import (
    AccountRow,
    TradeCaseExecutionRow,
    TradeCaseRiskBindingRow,
    TradeCaseRiskRequestRow,
)
from src.ledger.portfolio import portfolio_state
from src.orchestration.casefill.models import (
    ExecutionReading,
    ExecutionRefusal,
    ExecutionRefused,
    PaperFillRecorded,
    notional_of,
)
from src.orchestration.commander.context import SystemPausePort
from src.orchestration.costs.models import PaperCostAssumptions, PaperCostReading
from src.orchestration.paper import PaperOutcome, PaperTradingService
from src.orchestration.riskdata.context import RiskDataReader
from src.orchestration.riskdata.models import RiskDataReadiness
from src.orchestration.riskrequest.service import OneSnapshot, risk_market, too_old_for
from src.orchestration.sizing.context import base_asset_metadata, reference_price
from src.orchestration.workflow.engine import active_evidence
from src.orchestration.workflow.models import (
    TERMINAL_CASE_STATUSES,
    EvidenceType,
    TradeCase,
    TradeCaseStatus,
    WorkflowFailure,
)
from src.orchestration.workflow.service import TradeCaseService, case_from_row, risk_from_row
from src.risk.authorization import RiskAuthorization


class _Abort(Exception):  # noqa: N818 - carries a reading, not an error condition
    """Roll the transaction back and answer with this refusal.

    Some refusals are only discoverable once the execution has been started —
    the decision and its market are already staged when the execution boundary
    is reached. Returning normally would commit those; raising rolls them back,
    and the reading is handed out afterwards so the caller still gets a typed
    answer rather than an exception.
    """

    def __init__(self, refusal: ExecutionRefused) -> None:
        self.refusal = refusal
        super().__init__(refusal.reason.value)


class CaseFillUnavailable(Exception):
    """The call could not be attempted at all. Safe reason code only."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class CaseFillService:
    """Fills one stored, still-valid risk request, once."""

    sessions: async_sessionmaker[AsyncSession]
    cases: TradeCaseService
    paper: PaperTradingService
    markets: Any
    costs: PaperCostReading
    trading_mode: TradingMode = TradingMode.OBSERVE
    kill_switch: bool = False
    # Supplied by a deployment that also runs the accounting subsystem. Its
    # presence says the stop is configured; its *value* is never consulted,
    # because the locked account row is the authoritative read.
    pause: SystemPausePort | None = None
    clock: Clock = SystemClock()
    include_fixtures: bool = False

    @property
    def limits(self) -> RiskLimits:
        """SENTINEL's limits, from the one place that owns them.

        Read off the paper service rather than configured a second time here.
        Two settable copies could disagree, and the failure that produces is the
        worst available: the pre-checks refusing under one set while the
        evaluation that actually gates the fill runs under another, and the
        stricter of the two recorded in the basis as if it had applied.

        The account's durable pause is still folded in where the evaluation
        happens, inside `execute_in_session`, so a paused account tightens the
        same limits the rest of this call was checked against.
        """
        return self.paper.limits

    async def execute_case_fill(
        self,
        trade_case_id: UUID,
        *,
        request_key: str,
        expected_binding_id: UUID | None = None,
        expected_risk_input_digest: str | None = None,
    ) -> ExecutionReading:
        """Fill the stored request for one case, once, or say why not."""
        try:
            return await self._attempt(
                trade_case_id,
                request_key=request_key,
                expected_binding_id=expected_binding_id,
                expected_risk_input_digest=expected_risk_input_digest,
            )
        except _Abort as abort:
            # The transaction has rolled back; the answer survives it.
            return abort.refusal

    async def _attempt(
        self,
        trade_case_id: UUID,
        *,
        request_key: str,
        expected_binding_id: UUID | None = None,
        expected_risk_input_digest: str | None = None,
    ) -> ExecutionReading:
        feed = OneSnapshot(self.markets, self.include_fixtures)
        async with self.sessions.begin() as session:
            account = await session.scalar(
                select(AccountRow).where(AccountRow.id == 1).with_for_update()
            )
            if account is None:
                raise CaseFillUnavailable("PAPER_ACCOUNT_NOT_INITIALISED")
            try:
                row = await self.cases._locked_case(session, trade_case_id)
            except WorkflowFailure:
                raise CaseFillUnavailable("TRADE_CASE_NOT_FOUND") from None
            trade_case = case_from_row(row)

            request = await session.scalar(
                select(TradeCaseRiskRequestRow).where(
                    TradeCaseRiskRequestRow.trade_case_id == trade_case_id
                )
            )
            if request is None:
                return _refused(trade_case, ExecutionRefusal.REQUEST_NOT_FOUND)
            if request.request_key != request_key:
                # Two callers must not end up believing they own one order.
                return _refused(trade_case, ExecutionRefusal.REQUEST_KEY_MISMATCH)

            intent = TradeIntent.model_validate(request.basis["intent"])
            stored_fill = await session.scalar(
                select(TradeCaseExecutionRow).where(
                    TradeCaseExecutionRow.request_id == request.request_id
                )
            )
            # History first, and deliberately before every authorization check.
            # A completed fill is returned as it was recorded even once the
            # approval behind it has long expired: that is what already happened,
            # not a new permission to do it again.
            replayed = await self.paper.replay_in_session(session, intent)
            if replayed is not None:
                return _replay(trade_case, stored_fill, replayed)

            refusal = self._stops(trade_case, account)
            if refusal is not None:
                return refusal
            if (
                request.outcome != "APPROVE"
                or request.authorization != RiskAuthorization.APPROVED.value
            ):
                # `LIMITED` lands here too: a rejected size with a recorded
                # capacity is still a rejected size, and there is no downsize.
                return _refused(
                    trade_case,
                    ExecutionRefusal.REQUEST_NOT_APPROVED,
                    detail=request.authorization,
                )

            readiness = await RiskDataReader(
                cases=self.cases,
                markets=feed,
                costs=self.costs,
                clock=self.clock,
                pause=self.pause,
                include_fixtures=self.include_fixtures,
            ).readiness(trade_case_id)
            binding_row = await session.scalar(
                select(TradeCaseRiskBindingRow)
                .where(TradeCaseRiskBindingRow.trade_case_id == trade_case_id)
                .order_by(
                    TradeCaseRiskBindingRow.case_revision.desc(),
                    TradeCaseRiskBindingRow.recorded_at.desc(),
                    TradeCaseRiskBindingRow.binding_id.desc(),
                )
                .limit(1)
            )
            workflow = await self.cases.workflow_inputs_in_session(session, row)
            snapshot = await feed.latest(trade_case.market.pair_id)
            current = active_evidence(await self.cases.evidence(trade_case_id))
            positions = await self.paper.positions_in_session(session)

            # The last clock read, after the last input read. Everything from
            # here to the verdict is synchronous, so one instant governs
            # eligibility, the approval's own validity, every source age, the
            # UTC loss day and SENTINEL itself.
            now = self.clock.now()

            effective = self.cases.evaluate_inputs(workflow, now)
            if effective.status in TERMINAL_CASE_STATUSES:
                return _refused(
                    trade_case,
                    ExecutionRefusal.TRADE_CASE_TERMINAL,
                    readiness,
                    detail=effective.status.value,
                )
            if binding_row is None or binding_row.risk_decision_id != request.risk_decision_id:
                return _refused(trade_case, ExecutionRefusal.AUTHORIZATION_SUPERSEDED, readiness)
            binding = risk_from_row(binding_row)
            if expected_binding_id is not None and binding.binding_id != expected_binding_id:
                return _refused(trade_case, ExecutionRefusal.AUTHORIZATION_SUPERSEDED, readiness)
            if binding.authorization is not RiskAuthorization.APPROVED:
                return _refused(trade_case, ExecutionRefusal.REQUEST_NOT_APPROVED, readiness)
            if now >= binding.expires_at:
                # An approval issued with a short life is not an authorization
                # once that life is over, however unchanged everything else is.
                return _refused(trade_case, ExecutionRefusal.AUTHORIZATION_EXPIRED, readiness)
            if (
                effective.risk_input_digest != binding.risk_input_digest
                or request.risk_input_digest != binding.risk_input_digest
            ):
                # Not a weaker authorization: one about a different case.
                return _refused(trade_case, ExecutionRefusal.SAFETY_EVIDENCE_CHANGED, readiness)
            if (
                expected_risk_input_digest is not None
                and expected_risk_input_digest != binding.risk_input_digest
            ):
                return _refused(trade_case, ExecutionRefusal.SAFETY_EVIDENCE_CHANGED, readiness)
            if effective.status is not TradeCaseStatus.RISK_APPROVED:
                return _refused(
                    trade_case,
                    ExecutionRefusal.TRADE_CASE_NOT_AUTHORIZED,
                    readiness,
                    detail=effective.status.value,
                )
            if not readiness.complete:
                return _refused(trade_case, ExecutionRefusal.RISK_DATA_INCOMPLETE, readiness)
            if not readiness.is_current_at(now):
                return _refused(trade_case, ExecutionRefusal.DECISION_BASIS_EXPIRED, readiness)

            onchain = current.get(EvidenceType.ONCHAIN)
            anchor = current.get(EvidenceType.LIQUIDITY_EXECUTION)
            if snapshot is None or onchain is None or anchor is None:
                raise CaseFillUnavailable("CANONICAL_INPUTS_INCONSISTENT")
            price = reference_price(snapshot)
            metadata = base_asset_metadata(snapshot)
            if (
                price is None
                or metadata is None
                or not isinstance(self.costs, PaperCostAssumptions)
            ):
                raise CaseFillUnavailable("CANONICAL_INPUTS_INCONSISTENT")

            market = risk_market(
                base_asset_id=trade_case.market.base_asset_id,
                price=price,
                base_asset=metadata,
                snapshot=snapshot,
                onchain=onchain,
                anchor=anchor,
                costs=self.costs,
                correlation_id=trade_case.correlation_id,
                # Bound to the fill, not to the approval: this is a different
                # market reading at a different instant, and giving it the
                # request's identity would make two snapshots look like one.
                identity_key=f"{request.request_key}:fill",
            )
            stale = too_old_for(market, now, self.limits)
            if stale is not None:
                return _refused(
                    trade_case,
                    ExecutionRefusal.SOURCE_OLDER_THAN_RISK_LIMIT,
                    readiness,
                    detail=stale,
                )
            unmarked = portfolio_state(
                cash_usd=account.cash_usd,
                realized_loss_today_usd=account.realized_loss_today_usd,
                positions=positions,
                asset_id=market.asset_id,
                price_usd=market.price_usd,
                marks=None,
                now=now,
                max_snapshot_age_seconds=self.limits.max_snapshot_age_seconds,
                correlation_id=trade_case.correlation_id,
            ).unmarked_assets
            if unmarked:
                # SENTINEL would answer `PORTFOLIO_DATA_UNKNOWN`. A holding this
                # system cannot value is a missing capability, not a verdict.
                return _refused(trade_case, ExecutionRefusal.PORTFOLIO_MARKS_UNAVAILABLE, readiness)

            # The same risk evaluation, the same executor and the same
            # accounting the standalone paper path performs — inside this
            # transaction, so the fill and its case reference commit together.
            # A `PAUSE_SYSTEM` verdict sets the durable pause in here, with the
            # rejection it came with and without a fill.
            def still_authorised(at: datetime) -> str | None:
                """Every governing validity, re-checked at the actual boundary.

                Synchronous and over inputs already loaded under the locks, so
                nothing between this answer and the fill touches the database.
                The central evaluator is the one that answers about the case;
                this adds no second opinion about what a case is.
                """
                if at >= binding.expires_at:
                    return "AUTHORIZATION_EXPIRED"
                current_status = self.cases.evaluate_inputs(workflow, at)
                if current_status.status in TERMINAL_CASE_STATUSES:
                    return "TRADE_CASE_TERMINAL"
                if current_status.status is not TradeCaseStatus.RISK_APPROVED:
                    return "TRADE_CASE_NOT_AUTHORIZED"
                if current_status.risk_input_digest != binding.risk_input_digest:
                    return "SAFETY_EVIDENCE_CHANGED"
                if not readiness.is_current_at(at):
                    return "DECISION_BASIS_EXPIRED"
                return too_old_for(market, at, self.limits)

            outcome = await self.paper.execute_in_session(
                session,
                account,
                intent,
                market,
                positions=positions,
                marks=None,
                now=now,
                authorize=still_authorised,
            )
            if outcome.stop_reason is not None:
                # Approved, and the world moved on before it could be acted on.
                # Everything started is rolled back, and no artificial final
                # risk rejection is written in its place.
                raise _Abort(
                    _refused(
                        trade_case,
                        ExecutionRefusal.EXECUTION_WINDOW_EXPIRED,
                        readiness,
                        detail=outcome.stop_reason,
                    )
                )
            if outcome.fill is None or outcome.order is None:
                return _refused(
                    trade_case,
                    ExecutionRefusal.RISK_RECHECK_REFUSED,
                    readiness,
                    outcome=outcome.decision.outcome,
                    reason_codes=outcome.decision.reason_codes,
                )
            recorded = self._record(
                session,
                trade_case,
                request,
                binding.binding_id,
                outcome,
                market,
                now,
            )
            await self.cases.complete_execution_in_session(
                session,
                row,
                detail={
                    "request_id": str(request.request_id),
                    "execution_id": str(outcome.fill.id),
                    "recheck_decision_id": str(outcome.decision.id),
                    "intent_id": str(intent.id),
                },
            )
            return recorded.model_copy(update={"trade_case_status": TradeCaseStatus.EXECUTED.value})

    def _stops(self, trade_case: TradeCase, account: AccountRow) -> ExecutionRefused | None:
        """Every stop this path must honour, checked before anything else.

        A stop must not be lost by moving between the control plane, the risk
        request and the fill. All three read the same durable pause, and this
        one reads it from the account row it already holds locked.
        """
        if self.kill_switch or self.limits.kill_switch:
            return _refused(trade_case, ExecutionRefusal.KILL_SWITCH_ENGAGED)
        if self.trading_mode is not TradingMode.PAPER:
            return _refused(trade_case, ExecutionRefusal.KILL_SWITCH_ENGAGED, detail="OBSERVE")
        if self.pause is None:
            return _refused(trade_case, ExecutionRefusal.SYSTEM_STOP_UNREADABLE)
        if bool(account.paused):
            return _refused(trade_case, ExecutionRefusal.SYSTEM_PAUSED)
        return None

    def _record(
        self,
        session: AsyncSession,
        trade_case: TradeCase,
        request: TradeCaseRiskRequestRow,
        binding_id: UUID,
        outcome: PaperOutcome,
        market: Any,
        now: Any,
    ) -> PaperFillRecorded:
        """Bind the fill to the case, the request and the decision behind it."""
        fill = outcome.fill
        order = outcome.order
        decision = outcome.decision
        if fill is None or order is None:  # pragma: no cover - guarded by the caller
            raise CaseFillUnavailable("EXECUTION_NOT_FILLED")
        notional = notional_of(fill.quantity, fill.execution_price)
        case_execution_id = uuid5(
            NAMESPACE_URL, f"rh-agents:trade-case-execution:{request.request_key}"
        )
        basis = {
            "request_key": request.request_key,
            "trade_case_id": str(trade_case.id),
            "case_revision": trade_case.revision,
            "authorizing_binding_id": str(binding_id),
            "safety_risk_input_digest": request.risk_input_digest,
            # The market the re-check judged, whole. Without it the decision's
            # own `market_fingerprint` could never be checked again, and the
            # only way to learn what SENTINEL was shown would be to re-read
            # sources that have since moved.
            "market_snapshot": market.model_dump(mode="json"),
            "risk_limits": self.limits.model_dump(mode="json"),
            "cost_assumptions": self.costs.model_dump(mode="json"),
            "intent": request.basis["intent"],
            "recheck_decision": decision.model_dump(mode="json"),
            "fill": fill.model_dump(mode="json"),
        }
        session.add(
            TradeCaseExecutionRow(
                case_execution_id=case_execution_id,
                trade_case_id=trade_case.id,
                request_id=request.request_id,
                request_key=request.request_key,
                intent_id=fill.intent_id,
                order_id=order.id,
                execution_id=fill.id,
                authorizing_binding_id=binding_id,
                # The decision this order was actually built on, not a second
                # identifier derived from the request key. `OrderRow.risk_id`
                # and `RiskRow.id` are this same value.
                recheck_decision_id=decision.id,
                risk_input_digest=request.risk_input_digest,
                quantity=fill.quantity,
                execution_price_usd=fill.execution_price,
                notional_usd=notional,
                fees_usd=fill.fees_usd + fill.gas_usd,
                filled_at=fill.created_at,
                recorded_at=now,
                correlation_id=trade_case.correlation_id,
                basis=basis,
            )
        )
        return PaperFillRecorded(
            case_execution_id=case_execution_id,
            trade_case_id=trade_case.id,
            request_id=request.request_id,
            request_key=request.request_key,
            intent_id=fill.intent_id,
            order_id=order.id,
            execution_id=fill.id,
            authorizing_binding_id=binding_id,
            recheck_decision_id=decision.id,
            risk_input_digest=request.risk_input_digest,
            quantity=fill.quantity,
            execution_price_usd=fill.execution_price,
            notional_usd=notional,
            fees_usd=fill.fees_usd + fill.gas_usd,
            filled_at=fill.created_at,
            replayed=False,
            trade_case_status=trade_case.status.value,
        )


def _refused(
    trade_case: TradeCase,
    reason: ExecutionRefusal,
    readiness: RiskDataReadiness | None = None,
    *,
    outcome: Any = None,
    reason_codes: tuple[str, ...] = (),
    detail: str | None = None,
    replayed: bool = False,
) -> ExecutionRefused:
    from src.risk.authorization import classify_risk_authorization

    authorization = None
    if outcome is not None:
        authorization = classify_risk_authorization(
            outcome,
            reason_codes,
            position_size_limit_usd=Decimal("0"),
            max_additional_notional_usd=Decimal("0"),
        )
    return ExecutionRefused(
        reason=reason,
        trade_case_id=trade_case.id,
        trade_case_status=trade_case.status.value,
        outcome=outcome,
        authorization=authorization,
        reason_codes=reason_codes,
        data_gaps=() if readiness is None else readiness.gaps,
        blockers=() if readiness is None else readiness.blockers,
        detail=detail,
        replayed=replayed,
    )


def _replay(
    trade_case: TradeCase,
    stored: TradeCaseExecutionRow | None,
    outcome: ExecutionResult | RiskDecision,
) -> ExecutionReading:
    """Return what this order already produced, unchanged.

    A completed fill comes back as it was recorded even once the approval behind
    it has expired — that is what happened, not a new permission. A verdict that
    never became a fill comes back the same way: the order was decided, and one
    order gets one decision.
    """
    if isinstance(outcome, RiskDecision):
        return _refused(
            trade_case,
            ExecutionRefusal.RISK_RECHECK_REFUSED,
            outcome=outcome.outcome,
            reason_codes=outcome.reason_codes,
            replayed=True,
        )
    if stored is None:  # pragma: no cover - written in the same transaction
        raise CaseFillUnavailable("EXECUTION_NOT_BOUND_TO_CASE")
    return PaperFillRecorded(
        case_execution_id=stored.case_execution_id,
        trade_case_id=stored.trade_case_id,
        request_id=stored.request_id,
        request_key=stored.request_key,
        intent_id=stored.intent_id,
        order_id=stored.order_id,
        execution_id=stored.execution_id,
        authorizing_binding_id=stored.authorizing_binding_id,
        recheck_decision_id=stored.recheck_decision_id,
        risk_input_digest=stored.risk_input_digest,
        quantity=stored.quantity,
        execution_price_usd=stored.execution_price_usd,
        notional_usd=stored.notional_usd,
        fees_usd=stored.fees_usd,
        filled_at=aware(stored.filled_at),
        replayed=True,
        trade_case_status=trade_case.status.value,
    )
