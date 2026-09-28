"""The automatic sweep, end to end: a booked PAPER entry, a policy, one real exit.

The chain is the real one from `tests/paperexit`: approval, fill, ledger, and
the existing `PaperExitService` for the sale. The sale is judged on the exit's
own fresh ATLAS read (deterministic collector and policy, stub chain sources
observed at the exit instant) and the held market observed again. Only the
outside edge is a value. Policy numbers are test fixtures, not production
values.
"""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from src.agents.atlas.models import HolderSourceRow
from src.core.clock import FixedClock
from src.core.models import RiskLimits
from src.data.tables import PositionRow, TradeCaseExecutionRow
from src.markets.models import Availability
from src.orchestration.exitpolicy.policy import PaperExitPolicy
from src.orchestration.exitpolicy.service import AutoExitService
from src.orchestration.paperexit.exitread import AtlasExitRead, UnavailableExitRead
from tests.atlas.conftest import (
    builder_for,
    contract_facts,
    holder_rows,
    holder_source_result,
)
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

POLICY = PaperExitPolicy(stop_loss_bps=2000, take_profit_bps=5000, max_holding_seconds=6 * 3600)
LATER = timedelta(minutes=10)
FRESH = timedelta(seconds=5)
# One holder with 60 % of supply: far above SENTINEL's entry limit.
CONCENTRATED = tuple(
    sorted(
        (
            HolderSourceRow(address="0x" + "9a" * 20, balance_raw=600_000 * 10**18),
            *holder_rows(count=12, top_balance=20_000 * 10**18, step=1_000 * 10**18),
        ),
        key=lambda row: (-row.balance_raw, row.address),
    )
)


def fresh_read(at, **sources):
    """ATLAS's deterministic half, with the chain observed at the exit instant."""
    return AtlasExitRead(builder=builder_for(at, **sources), clock=FixedClock(at))


def sweeper(sessions, at, feed, *, read="fresh", policy=POLICY, **overrides):
    exit_read = fresh_read(at) if read == "fresh" else read
    return AutoExitService(
        sessions=sessions,
        exits=build_exit_service(sessions, at, feed=feed, exit_read=exit_read),
        markets=feed,
        policy=policy,
        limits=RiskLimits(),
        clock=FixedClock(at),
        **overrides,
    )


def observed(at, *, price=None, liquidity=None, age=FRESH):
    extra = {} if price is None else {"price": price}
    if liquidity is not None:
        extra["liquidity"] = liquidity
    return MultiMarkets(recorded_snapshot(at, age=age, metadata_age=age, **extra))


async def entry_of(sessions):
    async with sessions() as session:
        return await session.scalar(select(TradeCaseExecutionRow))


async def open_quantity(sessions):
    async with sessions() as session:
        return await session.scalar(select(PositionRow.quantity))


async def swept_once(sessions, at, feed, expected, **kwargs):
    result = await sweeper(sessions, at, feed, **kwargs).sweep()
    assert (result.evaluated, result.triggered, result.executed) == (1, 1, 1), result
    assert result.triggers == {expected: 1}
    rows = await exits(sessions)
    assert len(rows) == 1
    row = rows[0]
    assert (row.exit_trigger, row.exit_policy_version) == (expected, "PAPER_EXIT_V1")
    assert row.exit_trigger_basis["verdict"]["trigger"] == expected
    # Judged on the exit's own read, never on the entry's evidence.
    assert row.basis["exit_basis"] == "FRESH_EXIT_READ"
    assert row.basis["exit_onchain"]["source"] == "ATLAS_DETERMINISTIC_EXIT_READ"
    assert await open_quantity(sessions) == 0
    return row


async def refused_once(sessions, at, feed, refusal, **kwargs):
    result = await sweeper(sessions, at, feed, **kwargs).sweep()
    assert result.triggered == 1 and result.executed == 0, result
    assert result.refusals == {refusal: 1}
    assert await exits(sessions) == []
    assert await open_quantity(sessions) > 0
    return result


# ------------------------------------------------------- exits that execute


async def test_a_stop_ten_minutes_after_entry_executes_on_fresh_evidence(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + LATER
    row = await swept_once(sessions, at, observed(at, price=Decimal("0.9")), "STOP_LOSS")
    assert row.realized_pnl_usd < 0
    # The holder facts SENTINEL judged are the exit's, observed at the exit.
    holders = row.basis["market_snapshot"]["holders"]
    assert holders["created_at"].startswith(at.isoformat()[:19])


async def test_a_target_executes_at_a_profit(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + LATER
    row = await swept_once(sessions, at, observed(at, price=Decimal("2.0")), "TAKE_PROFIT")
    assert row.realized_pnl_usd > 0


async def test_a_time_exit_hours_after_entry_executes(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + timedelta(hours=6)
    row = await swept_once(sessions, at, observed(at), "TIME_EXIT")
    assert row.exit_trigger_basis["verdict"]["held_seconds"] == 6 * 3600


async def test_liquidity_below_the_entry_minimum_invalidates_and_sells(risk_db, now, trace):
    """The BUY floor no longer blocks the sale it was the reason for."""
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + LATER
    thin = observed(at, liquidity=Decimal("50000"))
    row = await swept_once(sessions, at, thin, "SENTINEL_INVALIDATION")
    assert Decimal(row.basis["market_snapshot"]["liquidity"]["liquidity_usd"]) == Decimal("50000")
    assert row.basis["decision"]["outcome"] == "APPROVE"


async def test_a_concentration_that_would_block_an_entry_does_not_block_the_exit(
    risk_db, now, trace
):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + LATER
    read = fresh_read(at, holders=holder_source_result(at, rows=CONCENTRATED))
    row = await swept_once(sessions, at, observed(at, price=Decimal("0.9")), "STOP_LOSS", read=read)
    top_ten = row.basis["market_snapshot"]["holders"]["top_ten_fraction"]
    assert Decimal(top_ten) > RiskLimits().max_top_ten_holder_fraction


# ------------------------------------------------------- fail closed


async def test_without_a_chain_source_the_exit_is_refused_by_name(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + LATER
    await refused_once(
        sessions,
        at,
        observed(at, price=Decimal("0.9")),
        "EXIT_READ_UNAVAILABLE",
        read=UnavailableExitRead("ONCHAIN_SOURCE_NOT_CONFIGURED"),
    )


async def test_stale_exit_holder_facts_refuse_the_sale(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + LATER
    old = holder_source_result(at, snapshot_timestamp=at - timedelta(minutes=2))
    await refused_once(
        sessions,
        at,
        observed(at, price=Decimal("0.9")),
        "SOURCE_OLDER_THAN_RISK_LIMIT",
        read=fresh_read(at, holders=old),
    )


async def test_missing_exit_holder_facts_refuse_the_sale(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + LATER
    missing = holder_source_result(at, status=Availability.UNAVAILABLE)
    await refused_once(
        sessions,
        at,
        observed(at, price=Decimal("0.9")),
        "EXIT_DATA_INCOMPLETE",
        read=fresh_read(at, holders=missing),
    )


async def test_a_stale_market_refuses_a_time_exit(risk_db, now, trace):
    """A time exit needs no price to trigger, but a sale needs a current one."""
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + timedelta(hours=6)
    await refused_once(
        sessions,
        at,
        observed(at, age=timedelta(minutes=2)),
        "SOURCE_OLDER_THAN_RISK_LIMIT",
    )


async def test_an_emptied_pool_refuses_the_sale(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + LATER
    result = await refused_once(
        sessions, at, observed(at, liquidity=Decimal("0")), "EXIT_RISK_REFUSED"
    )
    assert result.triggers == {"SENTINEL_INVALIDATION": 1}


async def test_a_token_contract_failure_still_vetoes_the_sale(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + LATER
    # No code at the token address any more: nothing there can be sold.
    proxied = contract_facts(code_present=False)
    await refused_once(
        sessions,
        at,
        observed(at, price=Decimal("0.9")),
        "EXIT_RISK_REFUSED",
        read=fresh_read(at, contract=proxied),
    )


async def test_the_entry_evidence_alone_is_never_a_later_basis(risk_db, now, trace):
    """A service composed without a fresh read refuses a late exit, fail closed."""
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + LATER
    await refused_once(
        sessions,
        at,
        market_feed(at, price=Decimal("0.5")),
        "SOURCE_OLDER_THAN_RISK_LIMIT",
        read=None,
    )


# ------------------------------------------------------- holding and budget


async def test_nothing_fires_inside_the_bands(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + LATER
    result = await sweeper(sessions, at, observed(at)).sweep()
    assert (result.evaluated, result.triggered, result.held) == (1, 0, 1)
    assert await exits(sessions) == []
    assert await open_quantity(sessions) > 0


async def test_a_stale_mark_never_triggers_a_price_exit(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + LATER
    stale = observed(at, price=Decimal("0.1"), age=timedelta(minutes=1))
    result = await sweeper(sessions, at, stale).sweep()
    assert (result.triggered, result.held) == (0, 1)
    assert await exits(sessions) == []


async def test_the_exit_budget_bounds_a_run(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + LATER
    result = await sweeper(sessions, at, observed(at, price=Decimal("0.9")), max_exits=0).sweep()
    assert result.triggered == 1 and result.executed == 0
    assert result.refusals == {"EXIT_BUDGET_REACHED": 1}
    assert await exits(sessions) == []


# ------------------------------------------------------- once, and the numbers


async def test_a_second_sweep_and_a_replay_sell_nothing_twice(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + LATER
    await swept_once(sessions, at, observed(at, price=Decimal("0.9")), "STOP_LOSS")
    # The same minute (a retry) and a later run: the position is closed.
    for instant in (at, at + timedelta(minutes=15)):
        again = await sweeper(sessions, instant, observed(instant, price=Decimal("0.9"))).sweep()
        assert again.evaluated == again.executed == 0
    assert len(await exits(sessions)) == 1


async def test_pnl_is_the_ledger_s_and_traceable_from_the_record(risk_db, now, trace):
    _, sessions = risk_db
    before = await read_account(sessions)
    await entered(sessions, now, trace)
    entry = await entry_of(sessions)
    at = now + timedelta(hours=2)
    row = await swept_once(sessions, at, observed(at, price=Decimal("2.0")), "TAKE_PROFIT")
    inputs = row.exit_trigger_basis["inputs"]
    assert Decimal(inputs["entry_price_usd"]) == entry.execution_price_usd
    assert Decimal(inputs["mark_price_usd"]) == Decimal("2.0")
    assert row.exit_trigger_basis["verdict"]["held_seconds"] == 2 * 3600
    assert row.execution_price_usd > 0 and row.filled_at is not None
    after = await read_account(sessions)
    assert money(after.cash_usd) > money(before.cash_usd)
    assert money(row.basis["trade"]["realized_pnl_usd"]) == money(row.realized_pnl_usd)
    # Proceeds net of fees minus the released cost basis: the ledger's identity.
    proceeds = row.quantity * row.execution_price_usd - row.fees_usd
    assert money(row.realized_pnl_usd) == money(proceeds - row.cost_basis_released_usd)
    assert row.realized_pnl_usd > 0
