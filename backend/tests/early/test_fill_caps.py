"""The early caps hold at the fill, not only at the approval.

An approval reserves nothing. Two early cases can each be approved against the
same free slot, and only the fill changes the ledger — so the fill is where the
strategy's caps must be judged again, under the account lock every fill takes,
on the ledger as it stands at that instant. Everything here runs the real
risk request and the real case fill.
"""

import asyncio
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from src.core.models import RiskOutcome
from src.data.tables import TradeCaseExecutionRow
from src.orchestration.casefill.models import ExecutionRefusal
from src.orchestration.strategy.early import EARLY_ENTRY_V1, cap_refusal, early_ledger
from src.orchestration.workflow.models import TradeCaseStatus
from tests.casefill.conftest import MultiMarkets, build_fill_service
from tests.early.test_sentinel import early_ready
from tests.paperexit.conftest import money
from tests.riskdata.conftest import configured_costs, market_for
from tests.riskrequest.conftest import build_service, fresh_snapshot, read_account, ready_case

MARKETS = tuple(
    market_for(token=f"{index + 0x31:02x}" * 20, pool=f"{index + 0x41:02x}" * 20)
    for index in range(6)
)


def feed_for(now):
    """Every early market plus the normal one, each freshly observed."""
    snapshots = [
        fresh_snapshot(
            now,
            pair_id=item.pair_id,
            base_asset_id=item.base_asset_id,
            label=f"early-market-{index}",
        )
        for index, item in enumerate(MARKETS)
    ]
    return MultiMarkets(fresh_snapshot(now), *snapshots)


class Book:
    """The real risk request and the real fill over one shared feed."""

    def __init__(self, sessions, now, *, costs=None) -> None:
        self.sessions = sessions
        self.now = now
        self.feed = feed_for(now)
        self.costs = costs
        extra = {} if costs is None else {"costs": costs}
        self.risk = build_service(sessions, now, feed=self.feed, notional="500", **extra)
        self.extra = extra

    def filler(self):
        return build_fill_service(self.sessions, self.now, feed=self.feed, **self.extra)

    async def approved(self, index, trace):
        key = f"early-{index}"
        trade_case = await early_ready(
            self.risk, self.sessions, self.now, trace, key=key, identity=MARKETS[index]
        )
        approval = await self.risk.request_risk_evaluation(trade_case.id, request_key=key)
        assert approval.kind == "risk_request_evaluated", approval
        assert approval.outcome is RiskOutcome.APPROVE, approval.reason_codes
        return trade_case

    async def fill(self, trade_case, index):
        return await self.filler().execute_case_fill(trade_case.id, request_key=f"early-{index}")

    async def entered(self, index):
        trade_case = await self.approved(index, uuid4())
        result = await self.fill(trade_case, index)
        assert result.kind == "paper_fill_recorded", getattr(result, "detail", None)
        return trade_case

    async def ledger(self):
        async with self.sessions() as session:
            return (await early_ledger(session)).at(self.now)


async def executions_of(sessions, trade_case_id):
    async with sessions() as session:
        return await session.scalar(
            select(func.count())
            .select_from(TradeCaseExecutionRow)
            .where(TradeCaseExecutionRow.trade_case_id == trade_case_id)
        )


def assert_within_caps(book):
    assert book.open_positions <= EARLY_ENTRY_V1.max_open_positions
    assert book.exposure_usd <= EARLY_ENTRY_V1.max_exposure_usd
    assert book.realized_loss_today_usd < EARLY_ENTRY_V1.daily_loss_cap_usd


async def test_two_approvals_for_the_last_exposure_slot_fill_only_once(risk_db, now):
    """Three entries held; A and B are both approved; A fills; B must not."""
    _, sessions = risk_db
    book = Book(sessions, now)
    for index in range(3):
        await book.entered(index)
    # Both approvals see the same free slot: 30.075 held, +10 fits under 50.
    first = await book.approved(3, uuid4())
    second = await book.approved(4, uuid4())

    filled = await book.fill(first, 3)
    assert filled.kind == "paper_fill_recorded", getattr(filled, "detail", None)
    # 40.10 held now; another ten would be 50.10.
    refused = await book.fill(second, 4)

    assert refused.kind == "execution_refused"
    assert refused.reason is ExecutionRefusal.EARLY_STRATEGY_CAP_REACHED
    assert refused.detail == "EARLY_MAX_EXPOSURE_REACHED"
    assert await executions_of(sessions, second.id) == 0
    ledger = await book.ledger()
    assert ledger.open_positions == 4
    assert_within_caps(ledger)
    # A strategy cap is not a verdict and stops nothing else.
    after = await book.risk.cases.get_trade_case(second.id)
    assert after.status is not TradeCaseStatus.RISK_REJECTED
    assert (await read_account(sessions)).paused is False


async def test_two_approvals_for_the_last_position_slot_fill_only_once(risk_db, now):
    """Without slippage or fees, the position count is the cap that binds."""
    _, sessions = risk_db
    book = Book(sessions, now, costs=configured_costs(fee="0", slippage="0"))
    for index in range(4):
        await book.entered(index)
    assert money((await book.ledger()).exposure_usd) == money(40)
    first = await book.approved(4, uuid4())
    second = await book.approved(5, uuid4())

    assert (await book.fill(first, 4)).kind == "paper_fill_recorded"
    refused = await book.fill(second, 5)

    assert refused.kind == "execution_refused"
    assert refused.reason is ExecutionRefusal.EARLY_STRATEGY_CAP_REACHED
    assert refused.detail == "EARLY_MAX_OPEN_POSITIONS_REACHED"
    ledger = await book.ledger()
    assert ledger.open_positions == 5
    assert money(ledger.exposure_usd) == money(50)
    assert_within_caps(ledger)


async def test_a_normal_fill_ignores_a_full_early_book(risk_db, now, trace):
    _, sessions = risk_db
    book = Book(sessions, now)
    for index in range(4):
        await book.entered(index)
    assert cap_refusal(await book.ledger()) is not None

    normal = await ready_case(book.risk.cases, now, trace, key="normal-case")
    approval = await book.risk.request_risk_evaluation(normal.id, request_key="normal")
    assert approval.outcome is RiskOutcome.APPROVE
    filled = await book.filler().execute_case_fill(normal.id, request_key="normal")

    assert filled.kind == "paper_fill_recorded", getattr(filled, "detail", None)


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required")
async def test_racing_fills_for_the_last_slot_never_both_book(risk_db, now):
    """Truly concurrent: the account row orders them, and the loser never books."""
    _, sessions = risk_db
    book = Book(sessions, now)
    for index in range(3):
        await book.entered(index)
    first = await book.approved(3, uuid4())
    second = await book.approved(4, uuid4())

    results = await asyncio.gather(book.fill(first, 3), book.fill(second, 4))

    kinds = sorted(item.kind for item in results)
    assert kinds == ["execution_refused", "paper_fill_recorded"]
    refused = next(item for item in results if item.kind == "execution_refused")
    # The loser either priced the portfolio before the winner's fill existed —
    # refused on that, before the cap is even reached — or sees the full book.
    assert refused.reason in (
        ExecutionRefusal.EARLY_STRATEGY_CAP_REACHED,
        ExecutionRefusal.PORTFOLIO_CHANGED_DURING_VALUATION,
    )
    ledger = await book.ledger()
    assert ledger.open_positions == 4
    assert_within_caps(ledger)


async def test_early_losses_realised_after_the_approval_stop_the_fill(risk_db, now):
    """Approved with no loss on the day; four early exits lose forty; no fill."""
    from tests.paperexit.conftest import build_exit_service, position_of

    _, sessions = risk_db
    costs = configured_costs(fee="0", slippage="0")
    book = Book(sessions, now, costs=costs)
    for index in range(4):
        await book.entered(index)
    late = await book.approved(4, uuid4())

    for index in range(4):
        identity = MARKETS[index]
        book.feed.replace(
            fresh_snapshot(
                now,
                pair_id=identity.pair_id,
                base_asset_id=identity.base_asset_id,
                label=f"early-crash-{index}",
                price=Decimal("0.0125"),
            )
        )
        position = await position_of(sessions, asset=identity.base_asset_id)
        sale = await build_exit_service(
            sessions, now, feed=book.feed, costs=costs
        ).execute_position_exit(position.id, request_key=f"early-exit-{index}")
        assert sale.kind == "paper_exit_recorded", getattr(sale, "reason", None)
    assert (await book.ledger()).realized_loss_today_usd >= EARLY_ENTRY_V1.daily_loss_cap_usd

    refused = await book.fill(late, 4)

    assert refused.kind == "execution_refused"
    assert refused.reason is ExecutionRefusal.EARLY_STRATEGY_CAP_REACHED
    assert refused.detail == "EARLY_DAILY_LOSS_CAP_REACHED"
    assert await executions_of(sessions, late.id) == 0
    assert (await read_account(sessions)).paused is False


async def test_with_paper_costs_the_exposure_cap_binds_at_four_entries(risk_db, now):
    """$50 is a hard cost-basis cap: slippage and fees count toward it.

    One $10 entry books 10 * 1.0025 * 1.003 = 10.055075 at the configured 25 bps
    slippage and 30 bps fees, so four hold 40.2203 and a fifth would pass 50.
    """
    _, sessions = risk_db
    book = Book(sessions, now)
    for index in range(4):
        await book.entered(index)
    ledger = await book.ledger()
    assert ledger.open_positions == 4
    assert money(ledger.exposure_usd) == money("40.2203")

    trade_case = await early_ready(
        book.risk, sessions, now, uuid4(), key="early-fifth", identity=MARKETS[4]
    )
    result = await book.risk.request_risk_evaluation(trade_case.id, request_key="early-fifth")

    assert result.kind == "risk_request_refused"
    assert result.detail == "EARLY_MAX_EXPOSURE_REACHED"
