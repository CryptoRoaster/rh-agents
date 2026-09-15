"""A position in one market, valued while a case runs in another.

The whole chain runs: a real fill in market A leaves a real position carrying
its own market identity, and a case in market B is then approved and filled with
that holding priced from A's own recorded observation. What is stubbed is the
market feed's read and the stop source — two ports, supplied as values.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from src.core.models import RiskLimits
from src.data.tables import (
    ExecutionRow,
    PositionRow,
    TradeCaseExecutionRow,
    TradeCaseRiskRequestRow,
)
from src.orchestration.casefill.models import ExecutionRefusal
from src.orchestration.riskrequest.models import RiskRequestRefusal
from src.orchestration.valuation.models import PortfolioValuation, PositionMark
from src.orchestration.workflow.models import TradeCaseStatus
from tests.casefill.conftest import MultiMarkets, approved_case, build_fill_service
from tests.riskdata.conftest import BASE_ASSET, market_for, recorded_snapshot
from tests.riskrequest.conftest import FRESH, build_service, read_account, set_account

# A second coherent market, so "a holding elsewhere" is a real market rather
# than a renamed copy of the one the case runs in.
SECOND = market_for(token="c7" * 20, pool="d8" * 20)


def both_markets(now, *, second_price=Decimal("7.5"), age=FRESH):
    primary = recorded_snapshot(now, age=age, metadata_age=age)
    other = recorded_snapshot(
        now,
        age=age,
        metadata_age=age,
        base_asset_id=SECOND.base_asset_id,
        pair_id=SECOND.pair_id,
        label="second",
        price=second_price,
    )
    return MultiMarkets(primary, other), primary, other


async def positions(sessions):
    async with sessions() as session:
        return (await session.scalars(select(PositionRow))).all()


async def fill_in(sessions, now, trace, *, identity, key, feed):
    """Carry one case all the way to a booked fill in the named market."""
    case, _, _ = await approved_case(sessions, now, trace, key=key, identity=identity, feed=feed)
    service = build_fill_service(sessions, now, feed=feed)
    result = await service.execute_case_fill(case.id, request_key=f"{key}-req")
    assert result.kind == "paper_fill_recorded", getattr(result, "reason", None)
    return case, result


# ------------------------------------------------- the position knows its market


async def test_a_filled_position_records_the_market_it_was_acquired_in(risk_db, now, trace):
    """Without this a later valuation would have to guess which pool it came from."""
    _, sessions = risk_db
    feed, _, _ = both_markets(now)
    await fill_in(sessions, now, trace, identity=SECOND, key="acq", feed=feed)

    held = await positions(sessions)
    assert len(held) == 1
    assert held[0].asset_id == SECOND.base_asset_id
    assert held[0].market_pair_id == SECOND.pair_id
    assert held[0].market_chain == SECOND.chain
    assert held[0].market_network == SECOND.network
    assert held[0].market_provider == SECOND.provider


# ------------------------------------------------- cross-market valuation


async def test_a_holding_in_market_a_is_valued_while_a_case_runs_in_market_b(risk_db, now, trace):
    """The whole point: exposure includes what is already held elsewhere."""
    _, sessions = risk_db
    feed, _, _ = both_markets(now)
    await fill_in(sessions, now, trace, identity=SECOND, key="first", feed=feed)

    second, result = await fill_in(sessions, now, uuid4(), identity=None, key="second", feed=feed)

    async with sessions() as session:
        row = await session.scalar(
            select(TradeCaseExecutionRow).where(TradeCaseExecutionRow.trade_case_id == second.id)
        )
    marks = {item["asset_id"]: item for item in row.basis["position_marks"]}
    assert SECOND.base_asset_id in marks, "the holding elsewhere was priced"
    assert Decimal(marks[SECOND.base_asset_id]["price_usd"]) == Decimal("7.5")
    assert marks[SECOND.base_asset_id]["pair_id"] == SECOND.pair_id
    assert (await positions(sessions)).__len__() == 2


async def test_the_exposure_the_recheck_judged_includes_the_other_holding(risk_db, now, trace):
    """Not merely recorded: the figure SENTINEL judged reflects it.

    The first entry is valued far above its cost, so an exposure that ignored it
    would be visibly different from one that did not.
    """
    _, sessions = risk_db
    feed, _, _ = both_markets(now, second_price=Decimal("7.5"))
    await fill_in(sessions, now, trace, identity=SECOND, key="expo", feed=feed)
    held = (await positions(sessions))[0]
    elsewhere = held.quantity * Decimal("7.5")

    second, _ = await fill_in(sessions, now, uuid4(), identity=None, key="expo2", feed=feed)

    async with sessions() as session:
        request = await session.scalar(
            select(TradeCaseRiskRequestRow).where(
                TradeCaseRiskRequestRow.trade_case_id == second.id
            )
        )
    exposure = Decimal(request.basis["portfolio"]["exposure_usd"])
    assert exposure >= elsewhere
    assert request.basis["portfolio"]["accounting"] == "PASS"
    assert any(
        item["asset_id"] == SECOND.base_asset_id
        for item in request.basis["portfolio"]["position_marks"]
    )


async def test_several_open_positions_are_all_valued(risk_db, now, trace):
    """Two holdings elsewhere, both priced, before a third case is filled."""
    third = market_for(token="e9" * 20, pool="fa" * 20)
    _, sessions = risk_db
    feed, primary, other = both_markets(now)
    feed.replace(
        recorded_snapshot(
            now,
            age=FRESH,
            metadata_age=FRESH,
            base_asset_id=third.base_asset_id,
            pair_id=third.pair_id,
            label="third",
            price=Decimal("3.25"),
        )
    )
    await fill_in(sessions, now, trace, identity=SECOND, key="a", feed=feed)
    await fill_in(sessions, now, uuid4(), identity=third, key="b", feed=feed)

    case, _ = await fill_in(sessions, now, uuid4(), identity=None, key="c", feed=feed)

    async with sessions() as session:
        row = await session.scalar(
            select(TradeCaseExecutionRow).where(TradeCaseExecutionRow.trade_case_id == case.id)
        )
    priced = {item["asset_id"] for item in row.basis["position_marks"]}
    assert {SECOND.base_asset_id, third.base_asset_id} <= priced
    assert len(await positions(sessions)) == 3


# ------------------------------------------------- typed stops, never a guess


@pytest.mark.parametrize(
    ("break_it", "expected"),
    [
        ("missing", "MARKET_NOT_RECORDED"),
        ("stale", "PRICE_STALE"),
        ("mismatched", "PRICE_ASSET_MISMATCH"),
    ],
)
async def test_an_unusable_holding_price_stops_the_fill(risk_db, now, trace, break_it, expected):
    """No entry price, no zero, no quietly stale observation."""
    _, sessions = risk_db
    feed, _, _ = both_markets(now)
    await fill_in(sessions, now, trace, identity=SECOND, key="brk-held", feed=feed)
    case, _, _ = await approved_case(sessions, now, uuid4(), key="brk", feed=feed)

    if break_it == "missing":
        feed.drop(SECOND.pair_id)
    elif break_it == "stale":
        aged = timedelta(seconds=120)
        feed.replace(
            recorded_snapshot(
                now,
                age=aged,
                metadata_age=aged,
                base_asset_id=SECOND.base_asset_id,
                pair_id=SECOND.pair_id,
                label="second",
            )
        )
    else:
        # A recording of the right market that prices the wrong asset.
        feed.replace(
            recorded_snapshot(
                now,
                age=FRESH,
                metadata_age=FRESH,
                base_asset_id=BASE_ASSET,
                pair_id=SECOND.pair_id,
                label="second",
            )
        )

    service = build_fill_service(sessions, now, feed=feed)
    result = await service.execute_case_fill(case.id, request_key="brk-req")

    assert result.kind == "execution_refused"
    assert result.reason is ExecutionRefusal.PORTFOLIO_MARKS_UNAVAILABLE
    assert result.detail == expected
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ExecutionRow)) == 1


async def test_a_risk_request_refuses_when_a_holding_cannot_be_valued(risk_db, now, trace):
    """The approval itself stops, so no unusable basis ever reaches a fill."""
    _, sessions = risk_db
    feed, _, _ = both_markets(now)
    await fill_in(sessions, now, trace, identity=SECOND, key="held", feed=feed)
    feed.drop(SECOND.pair_id)

    risk = build_service(sessions, now, feed=feed)
    from tests.riskrequest.conftest import ready_case

    case = await ready_case(risk.cases, now, uuid4(), key="unv-direct")
    result = await risk.request_risk_evaluation(case.id, request_key="unv-direct-req")

    assert result.kind == "risk_request_refused"
    assert result.reason is RiskRequestRefusal.PORTFOLIO_MARKS_UNAVAILABLE
    assert result.detail == "MARKET_NOT_RECORDED"


async def test_a_position_created_between_valuation_and_lock_stops_the_fill(
    risk_db, now, trace, monkeypatch
):
    """The portfolio moved while it was being valued, so it is not judged.

    A partial exposure figure is worse than none, because it looks like one.
    """
    from src.core.models import Position
    from src.data.repository import save_position

    _, sessions = risk_db
    feed, _, _ = both_markets(now)
    case, _, _ = await approved_case(sessions, now, trace, key="race", feed=feed)
    service = build_fill_service(sessions, now, feed=feed)

    original = type(service)._value_portfolio

    async def value_then_someone_buys(self, port):
        result = await original(self, port)
        async with sessions.begin() as session:
            await save_position(
                session,
                Position(
                    source="LEDGER",
                    correlation_id=trace,
                    asset_id=SECOND.base_asset_id,
                    market_pair_id=SECOND.pair_id,
                    market_chain=SECOND.chain,
                    market_network=SECOND.network,
                    market_provider=SECOND.provider,
                    quantity=Decimal("3"),
                    cost_basis_usd=Decimal("30"),
                    created_at=now,
                    updated_at=now,
                ),
            )
        return result

    monkeypatch.setattr(type(service), "_value_portfolio", value_then_someone_buys)
    result = await service.execute_case_fill(case.id, request_key="race-req")

    assert result.kind == "execution_refused"
    assert result.reason is ExecutionRefusal.PORTFOLIO_CHANGED_DURING_VALUATION
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ExecutionRow)) == 0


async def test_a_valuation_that_ages_out_before_the_fill_stops_it(risk_db, now, trace, monkeypatch):
    """Time passes while the decision is persisted, and marks age with it.

    The approval window is widened so that the *only* thing able to expire is
    the holding's valuation — otherwise the approval would run out first and
    prove nothing about marks.
    """

    from tests.casefill.test_hardening import WaitingClock, slow_persistence

    _, sessions = risk_db
    generous = RiskLimits(approval_ttl_seconds=600)
    # The case's own market stays fresh; the market the holding sits in is read
    # five seconds inside its limit, so a short wait carries it past.
    feed, _, _ = both_markets(now)
    feed.replace(
        recorded_snapshot(
            now,
            age=timedelta(seconds=25),
            metadata_age=timedelta(seconds=25),
            base_asset_id=SECOND.base_asset_id,
            pair_id=SECOND.pair_id,
            label="second",
        )
    )
    await fill_in(sessions, now, trace, identity=SECOND, key="age", feed=feed)
    case, approval, _ = await approved_case(
        sessions, now, uuid4(), key="age2", feed=feed, limits=generous
    )
    assert approval.kind == "risk_request_evaluated", getattr(approval, "reason", None)
    before = await read_account(sessions)

    clock = WaitingClock(now)
    service = build_fill_service(sessions, now, feed=feed, limits=generous, clock=clock)
    object.__setattr__(service.paper, "_clock", clock)
    object.__setattr__(service.cases, "clock", clock)
    slow_persistence(monkeypatch, clock, timedelta(seconds=3))

    result = await service.execute_case_fill(case.id, request_key="age2-req")

    assert result.kind == "execution_refused"
    assert result.reason is ExecutionRefusal.EXECUTION_WINDOW_EXPIRED
    assert result.detail == "POSITION_VALUATION_STALE"
    after = await read_account(sessions)
    assert (after.cash_usd, after.fees_paid_usd) == (before.cash_usd, before.fees_paid_usd)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ExecutionRow)) == 1
        assert len(await positions(sessions)) == 1


# ------------------------------------------------- audit and replay


async def test_the_stored_marks_reconstruct_after_a_reload(risk_db, now, trace):
    """Assignment, source times and provenance survive the round trip."""
    _, sessions = risk_db
    feed, _, other = both_markets(now)
    await fill_in(sessions, now, trace, identity=SECOND, key="audit", feed=feed)
    case, _ = await fill_in(sessions, now, uuid4(), identity=None, key="audit2", feed=feed)

    async with sessions() as session:
        row = await session.scalar(
            select(TradeCaseExecutionRow).where(TradeCaseExecutionRow.trade_case_id == case.id)
        )
    marks = [PositionMark.model_validate(item) for item in row.basis["position_marks"]]
    elsewhere = next(item for item in marks if item.asset_id == SECOND.base_asset_id)
    assert elsewhere.snapshot_id == other.id
    assert elsewhere.observation_id == other.price.id
    assert elsewhere.observed_at == other.price.observed_at
    assert elsewhere.provider == other.provider
    # Reconstructed from the row alone, with no market read at all.
    assert PortfolioValuation(marks=tuple(marks)).by_asset[SECOND.base_asset_id] == elsewhere


async def test_replay_works_with_no_market_data_at_all(risk_db, now, trace):
    """History needs no current prices, even with holdings to value."""
    _, sessions = risk_db
    feed, _, _ = both_markets(now)
    await fill_in(sessions, now, trace, identity=SECOND, key="hist", feed=feed)
    case, first = await fill_in(sessions, now, uuid4(), identity=None, key="hist2", feed=feed)

    class NoProvider:
        async def latest(self, identity, *, include_fixtures=False):
            raise AssertionError("replay must not reach the market layer")

    offline = build_fill_service(sessions, now, feed=NoProvider())
    again = await offline.execute_case_fill(case.id, request_key="hist2-req")

    assert again.kind == "paper_fill_recorded"
    assert again.replayed is True
    assert again.execution_id == first.execution_id


async def test_existing_stops_and_bookings_still_apply(risk_db, now, trace):
    """Pause, idempotency and the ledger are unchanged by the valuation."""
    _, sessions = risk_db
    feed, _, _ = both_markets(now)
    case, _, _ = await approved_case(sessions, now, trace, key="stops", feed=feed)
    await set_account(sessions, paused=True)
    service = build_fill_service(sessions, now, feed=feed)

    assert (
        await service.execute_case_fill(case.id, request_key="stops-req")
    ).reason is ExecutionRefusal.SYSTEM_PAUSED

    await set_account(sessions, paused=False)
    filled = await service.execute_case_fill(case.id, request_key="stops-req")
    assert filled.kind == "paper_fill_recorded"
    replay = await service.execute_case_fill(case.id, request_key="stops-req")
    assert replay.replayed is True and replay.execution_id == filled.execution_id
    assert (await service.cases.get_trade_case(case.id)).status is TradeCaseStatus.EXECUTED
