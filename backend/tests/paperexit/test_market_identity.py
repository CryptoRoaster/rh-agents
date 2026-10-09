"""A sale is judged only on a reading of the market the position is held in.

`PaperExitService` is the one exit boundary for normal and early exits alike.
The reading it prices, routes and asks SENTINEL about must be the held market's
own — same pool, chain, network and provider as the case that bought it. Any
other reading is refused by name, before anything is written: no order, no
fill, no exit row, no change to the account.
"""

import asyncio
import os
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from src.core.clock import FixedClock
from src.data.tables import ExecutionRow, OrderRow, TradeCaseExitRow
from src.markets.models import MarketSnapshot
from src.orchestration.paperexit.exitread import AtlasExitRead
from src.orchestration.paperexit.models import ExitRefusal
from tests.atlas.conftest import builder_for
from tests.paperexit.conftest import (
    build_exit_service,
    entered,
    market_feed,
    money,
    read_account,
    recorded_snapshot,
)

LATER = timedelta(minutes=10)
FRESH = timedelta(seconds=5)


def _swap(value, old, new, keys=None):
    """Every occurrence of `old`, as a whole value or as a prefix, replaced."""
    if isinstance(value, dict):
        return {key: _swap(item, old, new, keys) for key, item in value.items()}
    if isinstance(value, list):
        return [_swap(item, old, new, keys) for item in value]
    if isinstance(value, str):
        if value == old:
            return new
        if keys is None and value.startswith(f"{old}:"):
            return new + value[len(old) :]
    return value


def reading(at, *, price="0.90"):
    return recorded_snapshot(at, age=FRESH, metadata_age=FRESH, price=Decimal(price))


def foreign(snapshot, *, provider=None, network=None, chain=None):
    """The same observation as another provider, network or chain would state it."""
    payload = snapshot.model_dump(mode="json")
    if provider is not None:
        payload = _swap(payload, snapshot.provider, provider, keys={"provider"})
    if network is not None:
        prefix = f"{snapshot.chain}:{snapshot.network}"
        payload = _swap(payload, prefix, f"{snapshot.chain}:{network}")
        payload = _swap(payload, snapshot.network, network, keys={"network"})
    if chain is not None:
        prefix = f"{snapshot.chain}:{snapshot.network}"
        payload = _swap(payload, prefix, f"{chain}:{snapshot.network}")
        payload = _swap(payload, snapshot.chain, chain, keys={"chain"})
    return MarketSnapshot.model_validate(payload)


class Answering:
    """A market port that answers every question with one reading.

    The boundary under test must not trust the port to have answered the
    question it was asked.
    """

    def __init__(self, snapshot) -> None:
        self.snapshot = snapshot

    async def latest(self, identity, *, include_fixtures=False):
        return self.snapshot


def seller(sessions, at, feed, *, fresh=True):
    exit_read = AtlasExitRead(builder=builder_for(at), clock=FixedClock(at)) if fresh else None
    return build_exit_service(sessions, at, feed=feed, exit_read=exit_read)


async def nothing_written(sessions, cash_before):
    async with sessions() as session:
        exits = await session.scalar(select(func.count()).select_from(TradeCaseExitRow))
        sells = await session.scalar(select(func.count()).select_from(ExecutionRow))
        orders = await session.scalar(select(func.count()).select_from(OrderRow))
    account = await read_account(sessions)
    # One entry fill and its order exist; nothing for the refused sale.
    assert (exits, sells, orders) == (0, 1, 1)
    assert money(account.cash_usd) == money(cash_before)


@pytest.mark.parametrize(
    "difference",
    [{"provider": "another-provider"}, {"network": "testnet"}, {"chain": "bsc"}],
    ids=["provider", "network", "chain"],
)
@pytest.mark.parametrize("fresh", [True, False], ids=["fresh-exit-read", "entry-basis"])
async def test_a_reading_of_another_market_never_legitimises_a_sale(
    risk_db, now, trace, difference, fresh
):
    _, sessions = risk_db
    _, _, position = await entered(sessions, now, trace)
    cash = (await read_account(sessions)).cash_usd
    at = now + LATER
    feed = Answering(foreign(reading(at), **difference))

    result = await seller(sessions, at, feed, fresh=fresh).execute_position_exit(
        position.id, request_key="foreign"
    )

    assert result.kind == "exit_refused", result
    assert result.reason is ExitRefusal.EXIT_MARKET_IDENTITY_MISMATCH
    await nothing_written(sessions, cash)


async def test_the_held_market_s_own_reading_still_sells(risk_db, now, trace):
    _, sessions = risk_db
    _, _, position = await entered(sessions, now, trace)
    at = now + LATER

    result = await seller(
        sessions, at, market_feed(at, price=Decimal("0.90"))
    ).execute_position_exit(position.id, request_key="own")

    assert result.kind == "paper_exit_recorded", getattr(result, "reason", None)
    # SENTINEL's SELL verdict still decided it.
    async with sessions() as session:
        row = await session.scalar(select(TradeCaseExitRow))
    assert row.basis["exit_basis"] == "FRESH_EXIT_READ"
    assert row.basis["decision"]["outcome"] == "APPROVE"


async def test_a_refused_foreign_reading_is_retried_once_the_own_reading_exists(
    risk_db, now, trace
):
    _, sessions = risk_db
    _, _, position = await entered(sessions, now, trace)
    at = now + LATER
    refused = await seller(
        sessions, at, Answering(foreign(reading(at), provider="another-provider"))
    ).execute_position_exit(position.id, request_key="retry")
    assert refused.reason is ExitRefusal.EXIT_MARKET_IDENTITY_MISMATCH

    later = at + timedelta(minutes=1)
    own = market_feed(later, price=Decimal("0.90"))
    first = await seller(sessions, later, own).execute_position_exit(
        position.id, request_key="retry-2"
    )
    again = await seller(sessions, later, own).execute_position_exit(
        position.id, request_key="retry-3"
    )

    assert first.kind == "paper_exit_recorded"
    assert again.kind == "exit_refused"
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(TradeCaseExitRow)) == 1


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required")
async def test_racing_sales_on_own_and_foreign_readings_book_once(risk_db, now, trace):
    _, sessions = risk_db
    _, _, position = await entered(sessions, now, trace)
    at = now + LATER
    own = market_feed(at, price=Decimal("0.90"))
    alien = Answering(foreign(reading(at), provider="another-provider"))

    results = await asyncio.gather(
        seller(sessions, at, own).execute_position_exit(position.id, request_key="race-a"),
        seller(sessions, at, own).execute_position_exit(position.id, request_key="race-b"),
        seller(sessions, at, alien).execute_position_exit(position.id, request_key="race-c"),
    )

    assert sum(item.kind == "paper_exit_recorded" for item in results) == 1
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(TradeCaseExitRow)) == 1


async def test_the_normal_sweep_books_nothing_on_a_foreign_reading(risk_db, now, trace):
    from src.core.models import RiskLimits
    from src.orchestration.exitpolicy.policy import PaperExitPolicy
    from src.orchestration.exitpolicy.service import AutoExitService

    _, sessions = risk_db
    _, _, position = await entered(sessions, now, trace)
    cash = (await read_account(sessions)).cash_usd
    at = now + LATER
    # A crash and thin liquidity — as another provider reports the pool.
    feed = Answering(
        foreign(
            recorded_snapshot(
                at, age=FRESH, metadata_age=FRESH, price=Decimal("0.10"), liquidity=Decimal(1)
            ),
            provider="another-provider",
        )
    )
    sweep = AutoExitService(
        sessions=sessions,
        exits=seller(sessions, at, feed),
        markets=feed,
        policy=PaperExitPolicy(stop_loss_bps=2000, take_profit_bps=5000, max_holding_seconds=600),
        limits=RiskLimits(),
        clock=FixedClock(at),
    )

    result = await sweep.sweep()

    assert result.executed == 0, result
    await nothing_written(sessions, cash)
