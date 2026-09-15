"""One open position, closed by an explicit order, and every reason it is not.

The production path runs throughout: the real workflow service against a real
database, the real risk request, the real completeness check,
`src.risk.engine.evaluate`, `PaperExecutor` and the real ledger postings. The
entry is a real case-bound fill, not a fixture position.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from src.core.models import RiskLimits, RiskOutcome, Side
from src.data.tables import ExecutionRow, PositionRow, TradeRow
from src.orchestration.paperexit.models import ExitRefusal
from tests.paperexit.conftest import (
    build_exit_service,
    entered,
    exits,
    market_feed,
    money,
    position_of,
    read_account,
    set_account,
)

BPS = Decimal("10000")


def expected_sale(quantity, quote, *, slippage_bps, fee_bps):
    """What a sale of this size must produce, computed independently.

    Deliberately not a call into the executor or the ledger: a test that asks
    the code under test what the answer is proves only that it is consistent
    with itself.
    """
    price = (quantity * Decimal("0") + quote) * (1 - slippage_bps / BPS)
    price = price.quantize(Decimal("0.000000000000000001"))
    gross = (quantity * price).quantize(Decimal("0.000000000000000001"))
    fees = (quantity * price * fee_bps / BPS).quantize(Decimal("0.000000000000000001"))
    return price, gross, fees


async def costs_in_force():
    from tests.riskdata.conftest import configured_costs

    assumptions = configured_costs()
    return assumptions.slippage_bps, assumptions.fee_bps


# ------------------------------------------------------- the sale itself


async def test_an_entry_is_closed_completely(risk_db, now, trace):
    """Quantity nil, cost basis released, cash and fees booked."""
    _, sessions = risk_db
    feed = market_feed(now)
    case, entry, position = await entered(sessions, now, trace, feed=feed)
    before = await read_account(sessions)
    slippage, fee = await costs_in_force()
    price, gross, fees = expected_sale(
        position.quantity, Decimal("1.25"), slippage_bps=slippage, fee_bps=fee
    )

    service = build_exit_service(sessions, now, feed=feed)
    result = await service.execute_position_exit(position.id, request_key="exit-one")

    assert result.kind == "paper_exit_recorded", getattr(result, "reason", None)
    assert result.closes_position is True
    assert result.is_simulated is True
    assert result.quantity == position.quantity
    assert money(result.execution_price_usd) == money(price)
    assert money(result.fees_usd) == money(fees)
    assert money(result.cost_basis_released_usd) == money(position.cost_basis_usd)

    after = await read_account(sessions)
    assert money(after.cash_usd) == money(before.cash_usd + gross - fees)
    assert money(after.fees_paid_usd) == money(before.fees_paid_usd + fees)
    closed = await position_of(sessions)
    assert closed.quantity == Decimal("0")
    assert closed.cost_basis_usd == Decimal("0")
    # The entry is untouched history; the exit is its own record.
    assert result.trade_case_id == case.id
    assert result.case_execution_id == entry.case_execution_id


@pytest.mark.parametrize(
    ("quote", "winning"),
    [(Decimal("4"), True), (Decimal("0.4"), False)],
)
async def test_the_realised_result_matches_an_independent_calculation(
    risk_db, now, trace, quote, winning
):
    """Gain and loss both, checked against arithmetic done outside the code."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)
    slippage, fee = await costs_in_force()
    # The market moves before the exit; the entry's cost basis does not.
    feed.replace(recorded(now, quote))
    price, gross, fees = expected_sale(position.quantity, quote, slippage_bps=slippage, fee_bps=fee)
    expected = gross - fees - position.cost_basis_usd
    assert (expected > 0) is winning

    service = build_exit_service(sessions, now, feed=feed)
    result = await service.execute_position_exit(position.id, request_key="exit-pnl")

    assert result.kind == "paper_exit_recorded", getattr(result, "reason", None)
    assert money(result.realized_pnl_usd) == money(expected)
    sale = await sell_trade(sessions)
    async with sessions() as session:
        closed = await session.scalar(select(PositionRow))
    assert money(sale.realized_pnl_usd) == money(expected)
    assert money(closed.realized_pnl_usd) == money(expected)
    account = await read_account(sessions)
    assert money(account.realized_loss_today_usd) == money(max(Decimal("0"), -expected))


async def sell_trades(sessions):
    """Every booked SELL, read out of the ledger's own records."""
    from src.core.models import Trade

    async with sessions() as session:
        rows = (await session.scalars(select(TradeRow))).all()
    trades = [Trade.model_validate(row.payload) for row in rows]
    return [item for item in trades if item.side is Side.SELL]


async def sell_trade(sessions):
    found = await sell_trades(sessions)
    assert len(found) == 1
    return found[0]


def recorded(now, price):
    from tests.paperexit.conftest import FRESH, recorded_snapshot

    return recorded_snapshot(now, age=FRESH, metadata_age=FRESH, price=price)


async def test_nothing_is_oversold_and_no_quantity_goes_negative(risk_db, now, trace):
    """A second order finds nothing held, and says so."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)
    service = build_exit_service(sessions, now, feed=feed)
    assert (
        await service.execute_position_exit(position.id, request_key="exit-a")
    ).kind == "paper_exit_recorded"

    again = await service.execute_position_exit(position.id, request_key="exit-b")

    assert again.kind == "exit_refused"
    assert again.reason is ExitRefusal.POSITION_ALREADY_CLOSED
    closed = await position_of(sessions)
    assert closed.quantity == Decimal("0")
    assert len(await exits(sessions)) == 1
    assert len(await sell_trades(sessions)) == 1


# ------------------------------------------------------- identity and replay


async def test_the_same_order_replays_unchanged_without_reading_the_market(risk_db, now, trace):
    """Even once the approval is long gone, and with the market unreachable."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)
    first = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        position.id, request_key="exit-replay"
    )
    assert first.kind == "paper_exit_recorded"

    offline = Unreachable()
    later = build_exit_service(sessions, now + timedelta(hours=3), feed=offline)
    again = await later.execute_position_exit(position.id, request_key="exit-replay")

    assert again.kind == "paper_exit_recorded"
    assert again.replayed is True
    assert again.execution_id == first.execution_id
    assert money(again.realized_pnl_usd) == money(first.realized_pnl_usd)
    assert offline.reads == 0
    assert len(await exits(sessions)) == 1


async def test_a_stored_refusal_replays_the_same_way(risk_db, now, trace):
    """One order gets one verdict, and a refused sale is not retried into a yes."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)
    # A limit the sale cannot meet, so SENTINEL refuses this SELL on its merits.
    strict = RiskLimits(min_liquidity_usd=Decimal("999999999"))
    refused = await build_exit_service(
        sessions, now, feed=feed, limits=strict
    ).execute_position_exit(position.id, request_key="exit-no")
    assert refused.reason is ExitRefusal.EXIT_RISK_REFUSED
    assert "INSUFFICIENT_LIQUIDITY" in refused.reason_codes

    offline = Unreachable()
    again = await build_exit_service(sessions, now, feed=offline).execute_position_exit(
        position.id, request_key="exit-no"
    )

    assert again.reason is ExitRefusal.EXIT_RISK_REFUSED
    assert again.replayed is True
    assert again.reason_codes == refused.reason_codes
    assert offline.reads == 0
    assert (await position_of(sessions)).quantity == position.quantity
    assert await exits(sessions) == []


async def test_a_key_that_names_another_position_is_refused(risk_db, now, trace):
    """Two callers must not end up believing they own the same order."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)
    service = build_exit_service(sessions, now, feed=feed)
    assert (
        await service.execute_position_exit(position.id, request_key="exit-key")
    ).kind == "paper_exit_recorded"

    other = await elsewhere(sessions, now, trace)
    result = await service.execute_position_exit(other, request_key="exit-key")

    assert result.reason is ExitRefusal.EXIT_KEY_MISMATCH
    assert len(await exits(sessions)) == 1


async def elsewhere(sessions, now, trace):
    """A second holding, in its own market, that no exit here has touched."""
    from src.core.models import Position
    from src.data.repository import save_position
    from tests.paperexit.conftest import market_for

    market = market_for(token="c7" * 20, pool="d8" * 20)
    async with sessions.begin() as session:
        stored = await save_position(
            session,
            Position(
                source="LEDGER",
                correlation_id=trace,
                asset_id=market.base_asset_id,
                market_pair_id=market.pair_id,
                market_chain=market.chain,
                market_network=market.network,
                market_provider=market.provider,
                quantity=Decimal("3"),
                cost_basis_usd=Decimal("30"),
                created_at=now,
                updated_at=now,
            ),
        )
    return stored.id if stored is not None else (await second_position(sessions, market))


async def second_position(sessions, market):
    async with sessions() as session:
        row = await session.scalar(
            select(PositionRow).where(PositionRow.asset_id == market.base_asset_id)
        )
    return row.id


class Unreachable:
    """A market layer that fails on contact. Replay must never touch it."""

    def __init__(self) -> None:
        self.reads = 0

    async def latest(self, identity, *, include_fixtures=False):
        self.reads += 1
        raise AssertionError(f"the market layer was read for {identity}")


# ------------------------------------------------------- provenance


async def test_a_holding_with_no_case_bound_entry_is_not_sold(risk_db, now, trace):
    """Selling something this system cannot say it bought has no origin."""
    _, sessions = risk_db
    feed = market_feed(now)
    await entered(sessions, now, trace, feed=feed)
    orphan = await elsewhere(sessions, now, trace)

    result = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        orphan, request_key="exit-orphan"
    )

    assert result.reason is ExitRefusal.POSITION_ORIGIN_UNKNOWN
    assert await exits(sessions) == []


async def test_a_holding_with_no_recorded_market_is_not_sold(risk_db, now, trace):
    """No market, nothing to sell in one."""
    from sqlalchemy import update

    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)
    async with sessions.begin() as session:
        await session.execute(
            update(PositionRow).where(PositionRow.id == position.id).values(market_pair_id=None)
        )

    result = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        position.id, request_key="exit-blank"
    )

    assert result.reason is ExitRefusal.POSITION_MARKET_UNKNOWN
    assert await exits(sessions) == []


async def test_a_holding_attributed_to_another_market_is_not_sold(risk_db, now, trace):
    """The position and its entry disagree; a sale is not where that is settled."""
    from sqlalchemy import update

    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)
    async with sessions.begin() as session:
        await session.execute(
            update(PositionRow)
            .where(PositionRow.id == position.id)
            .values(market_provider="somebody-else")
        )

    result = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        position.id, request_key="exit-wrong"
    )

    assert result.reason in (
        ExitRefusal.POSITION_MARKET_MISMATCH,
        ExitRefusal.POSITION_ORIGIN_UNKNOWN,
    )
    assert await exits(sessions) == []
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ExecutionRow)) == 1


async def test_a_position_that_does_not_exist_is_refused(risk_db, now, trace):
    _, sessions = risk_db
    feed = market_feed(now)
    await entered(sessions, now, trace, feed=feed)

    result = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        uuid4(), request_key="exit-ghost"
    )

    assert result.reason is ExitRefusal.POSITION_NOT_FOUND


# ------------------------------------------------------- stops and data


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"pause": None}, ExitRefusal.SYSTEM_STOP_UNREADABLE),
        ({"kill_switch": True}, ExitRefusal.KILL_SWITCH_ENGAGED),
        ({"trading_mode": "OBSERVE"}, ExitRefusal.KILL_SWITCH_ENGAGED),
    ],
)
async def test_every_stop_blocks_the_exit_too(risk_db, now, trace, overrides, expected):
    """No emergency path. A stop that does not stop a sale is not a stop."""
    from src.core.models import TradingMode

    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)
    if overrides.get("trading_mode") == "OBSERVE":
        overrides = {"trading_mode": TradingMode.OBSERVE}
    service = build_exit_service(sessions, now, feed=feed, **overrides)

    result = await service.execute_position_exit(position.id, request_key="exit-stop")

    assert result.reason is expected
    assert (await position_of(sessions)).quantity == position.quantity
    assert await exits(sessions) == []


async def test_a_paused_account_blocks_the_exit(risk_db, now, trace):
    """Read from the locked row, like every other reader of this stop."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)
    await set_account(sessions, paused=True)

    result = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        position.id, request_key="exit-paused"
    )

    assert result.reason is ExitRefusal.SYSTEM_PAUSED
    assert await exits(sessions) == []

    await set_account(sessions, paused=False)
    allowed = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        position.id, request_key="exit-paused-2"
    )
    assert allowed.kind == "paper_exit_recorded"


async def test_a_pause_verdict_at_exit_time_is_stored_with_its_rejection(risk_db, now, trace):
    """The stop lands atomically with the verdict it came with, and no sale."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)
    await set_account(sessions, realized_loss_today_usd=Decimal("600"), loss_day=now.date())

    result = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        position.id, request_key="exit-pause"
    )

    assert result.reason is ExitRefusal.EXIT_RISK_REFUSED
    assert result.outcome is RiskOutcome.PAUSE_SYSTEM
    assert "DAILY_LOSS_LIMIT" in result.reason_codes
    assert (await read_account(sessions)).paused is True
    assert (await position_of(sessions)).quantity == position.quantity
    assert await exits(sessions) == []


async def test_a_stale_market_is_a_typed_stop(risk_db, now, trace):
    """Present, provable and still older than SENTINEL's own bound."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)
    aged = timedelta(seconds=120)
    feed.replace(recorded_at(now, aged))

    result = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        position.id, request_key="exit-stale"
    )

    assert result.reason in (
        ExitRefusal.SOURCE_OLDER_THAN_RISK_LIMIT,
        ExitRefusal.RISK_DATA_INCOMPLETE,
    )
    assert await exits(sessions) == []


def recorded_at(now, age):
    from tests.paperexit.conftest import recorded_snapshot

    return recorded_snapshot(now, age=age, metadata_age=age)


async def test_a_missing_market_is_a_typed_stop(risk_db, now, trace):
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position = await entered(sessions, now, trace, feed=feed)
    feed.drop(IDENTITY.pair_id)

    result = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        position.id, request_key="exit-missing"
    )

    assert result.reason is ExitRefusal.RISK_DATA_INCOMPLETE
    assert await exits(sessions) == []


from tests.paperexit.conftest import IDENTITY  # noqa: E402
