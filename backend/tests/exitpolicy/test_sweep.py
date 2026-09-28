"""The automatic sweep, end to end: a booked PAPER entry, a policy, one real exit.

The chain is the real one from `tests/paperexit`: approval, fill, ledger, and
the existing `PaperExitService` for the sale. Only the market read is a value.
Policy numbers are test fixtures, not production values.

The existing exit judges the sale on the case's ATLAS evidence, which SENTINEL
accepts for `max_snapshot_age_seconds` (30 s by default) and which a filled,
terminal case cannot refresh. So a triggered exit executes inside that window
and is refused, fail closed, after it; both are tested. The time exit therefore
uses a fixture holding time of seconds.
"""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from src.core.clock import FixedClock
from src.core.models import RiskLimits
from src.data.tables import PositionRow, TradeCaseExecutionRow
from src.orchestration.exitpolicy.policy import PaperExitPolicy
from src.orchestration.exitpolicy.service import AutoExitService
from tests.casefill.conftest import MultiMarkets
from tests.paperexit.conftest import (
    build_exit_service,
    entered,
    exits,
    market_feed,
    money,
    read_account,
    recorded_snapshot,
)

POLICY = PaperExitPolicy(stop_loss_bps=2000, take_profit_bps=5000, max_holding_seconds=15)
SOON = timedelta(seconds=10)


def sweeper(sessions, at, feed, policy=POLICY, **overrides):
    return AutoExitService(
        sessions=sessions,
        exits=build_exit_service(sessions, at, feed=feed),
        markets=feed,
        policy=policy,
        limits=RiskLimits(),
        clock=FixedClock(at),
        **overrides,
    )


async def entry_of(sessions):
    async with sessions() as session:
        return await session.scalar(select(TradeCaseExecutionRow))


async def open_quantity(sessions):
    async with sessions() as session:
        return await session.scalar(select(PositionRow.quantity))


async def swept_once(sessions, at, feed, expected):
    result = await sweeper(sessions, at, feed).sweep()
    assert (result.evaluated, result.triggered, result.executed) == (1, 1, 1), result
    assert result.triggers == {expected: 1}
    rows = await exits(sessions)
    assert len(rows) == 1
    row = rows[0]
    assert (row.exit_trigger, row.exit_policy_version) == (expected, "PAPER_EXIT_V1")
    assert row.exit_trigger_basis["verdict"]["trigger"] == expected
    assert await open_quantity(sessions) == 0
    return row


async def test_a_mark_below_the_stop_sells_at_a_loss(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    later = now + SOON
    row = await swept_once(sessions, later, market_feed(later, price=Decimal("0.9")), "STOP_LOSS")
    assert row.realized_pnl_usd < 0


async def test_a_mark_above_the_target_sells_at_a_profit(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    later = now + SOON
    row = await swept_once(sessions, later, market_feed(later, price=Decimal("2.0")), "TAKE_PROFIT")
    assert row.realized_pnl_usd > 0


async def test_the_holding_time_ends_a_quiet_position(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    later = now + timedelta(seconds=15)
    await swept_once(sessions, later, market_feed(later), "TIME_EXIT")


async def test_liquidity_below_sentinel_minimum_triggers_but_sentinel_refuses_the_sale(
    risk_db, now, trace
):
    """The invalidation is detected; the SELL is still SENTINEL's to allow.

    SENTINEL's minimum liquidity binds sales too, so the very condition that
    invalidates the position also rejects its exit. Nothing is bypassed: the
    trigger is counted, the refusal is reported, the position stays open.
    """
    _, sessions = risk_db
    await entered(sessions, now, trace)
    later = now + SOON
    thin = MultiMarkets(
        recorded_snapshot(
            later,
            age=timedelta(seconds=5),
            metadata_age=timedelta(seconds=5),
            liquidity=Decimal("50000"),
        )
    )
    result = await sweeper(sessions, later, thin).sweep()
    assert result.triggers == {"SENTINEL_INVALIDATION": 1}
    assert result.executed == 0
    assert result.refusals == {"EXIT_RISK_REFUSED": 1}
    assert await exits(sessions) == []
    assert await open_quantity(sessions) > 0


async def test_nothing_fires_inside_the_bands(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    later = now + SOON
    result = await sweeper(sessions, later, market_feed(later)).sweep()
    assert (result.evaluated, result.triggered, result.held) == (1, 0, 1)
    assert await exits(sessions) == []
    assert await open_quantity(sessions) > 0


async def test_a_stale_mark_never_triggers_a_price_exit(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    later = now + SOON
    stale = market_feed(later, price=Decimal("0.1"), age=timedelta(minutes=1))
    result = await sweeper(sessions, later, stale).sweep()
    assert (result.triggered, result.held) == (0, 1)
    assert await exits(sessions) == []


async def test_a_second_sweep_and_a_replay_sell_nothing_twice(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    later = now + SOON
    feed = market_feed(later, price=Decimal("0.9"))
    await swept_once(sessions, later, feed, "STOP_LOSS")
    # The same minute (a retry) and a later run: the position is closed.
    for at in (later, later + timedelta(seconds=5)):
        again = await sweeper(sessions, at, market_feed(at, price=Decimal("0.9"))).sweep()
        assert again.evaluated == again.executed == 0
    assert len(await exits(sessions)) == 1


async def test_pnl_is_the_ledger_s_and_traceable_from_the_record(risk_db, now, trace):
    _, sessions = risk_db
    before = await read_account(sessions)
    await entered(sessions, now, trace)
    entry = await entry_of(sessions)
    later = now + SOON
    row = await swept_once(sessions, later, market_feed(later, price=Decimal("2.0")), "TAKE_PROFIT")
    inputs = row.exit_trigger_basis["inputs"]
    assert Decimal(inputs["entry_price_usd"]) == entry.execution_price_usd
    assert Decimal(inputs["mark_price_usd"]) == Decimal("2.0")
    assert row.exit_trigger_basis["verdict"]["held_seconds"] == 10
    assert row.execution_price_usd > 0 and row.filled_at is not None
    # Realised PnL is what the ledger booked: cash back to start plus the gain.
    after = await read_account(sessions)
    assert money(after.cash_usd) > money(before.cash_usd)
    assert money(row.basis["trade"]["realized_pnl_usd"]) == money(row.realized_pnl_usd)
    # Proceeds net of fees minus the released cost basis: the ledger's identity.
    proceeds = row.quantity * row.execution_price_usd - row.fees_usd
    assert money(row.realized_pnl_usd) == money(proceeds - row.cost_basis_released_usd)
    assert row.realized_pnl_usd > 0


async def test_the_exit_budget_bounds_a_run(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    later = now + SOON
    feed = market_feed(later, price=Decimal("0.9"))
    result = await sweeper(sessions, later, feed, max_exits=0).sweep()
    assert result.triggered == 1 and result.executed == 0
    assert result.refusals == {"EXIT_BUDGET_REACHED": 1}
    assert await exits(sessions) == []


async def test_an_aged_entry_basis_refuses_the_sale_and_sells_nothing(risk_db, now, trace):
    """Triggered, then refused by the existing exit: fail closed, no bypass."""
    _, sessions = risk_db
    await entered(sessions, now, trace)
    later = now + timedelta(minutes=10)
    feed = market_feed(later, price=Decimal("0.5"))
    result = await sweeper(sessions, later, feed).sweep()
    assert result.triggers == {"STOP_LOSS": 1}
    assert result.executed == 0
    assert result.refusals == {"SOURCE_OLDER_THAN_RISK_LIMIT": 1}
    assert await exits(sessions) == []
    assert await open_quantity(sessions) > 0
