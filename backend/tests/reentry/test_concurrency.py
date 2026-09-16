"""Two callers, one completed cycle, and the guarantees that survive them.

PostgreSQL only. Row locks are what these prove, and SQLite has none — a test
that ran there would report a pass it had not earned.
"""

import asyncio
import os
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from src.data.tables import TradeCaseRow, TradeCycleRow
from src.orchestration.reentry.models import ReentryRefusal
from tests.reentry.conftest import (
    build_reentry_service,
    closed_cycle,
    cycles,
    market_feed,
    read_account,
    set_account,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required"
)


async def counts(sessions):
    async with sessions() as session:
        made = await session.scalar(select(func.count()).select_from(TradeCycleRow))
        cases = await session.scalar(select(func.count()).select_from(TradeCaseRow))
    return made, cases


async def test_two_identical_calls_open_one_cycle_and_replay_the_other(risk_db, now, trace):
    """One key, one successor. The second caller gets it back, not another."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, _, sale = await closed_cycle(sessions, now, trace, feed=feed)

    results = await asyncio.gather(
        build_reentry_service(sessions, now).open_reentry(sale.exit_id, request_key="race"),
        build_reentry_service(sessions, now).open_reentry(sale.exit_id, request_key="race"),
    )

    assert {item.kind for item in results} == {"reentry_opened"}
    assert {item.trade_case_id for item in results} == {results[0].trade_case_id}
    assert {item.cycle_id for item in results} == {results[0].cycle_id}
    assert sorted(item.replayed for item in results) == [False, True]
    assert await counts(sessions) == (2, 2)


async def test_two_different_keys_open_at_most_one_successor(risk_db, now, trace):
    """A completed exit has one successor, whoever asks and however they ask."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, _, sale = await closed_cycle(sessions, now, trace, feed=feed)

    results = await asyncio.gather(
        build_reentry_service(sessions, now).open_reentry(sale.exit_id, request_key="one"),
        build_reentry_service(sessions, now).open_reentry(sale.exit_id, request_key="two"),
        return_exceptions=True,
    )

    opened = [item for item in results if getattr(item, "kind", None) == "reentry_opened"]
    refused = [item for item in results if getattr(item, "kind", None) == "reentry_refused"]
    assert len(opened) == 1
    # The loser either saw the winner's row or lost the unique constraint; both
    # are one successor, and neither is a second one.
    assert len(refused) + len([item for item in results if isinstance(item, Exception)]) == 1
    if refused:
        assert refused[0].reason is ReentryRefusal.SUCCESSOR_ALREADY_EXISTS
    assert await counts(sessions) == (2, 2)


async def test_a_pause_racing_a_re_entry_leaves_neither_stepping_over_the_other(
    risk_db, now, trace
):
    """Both go through the account row, so the order is decided, not chanced."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, _, sale = await closed_cycle(sessions, now, trace, feed=feed)

    async def pause():
        await set_account(sessions, paused=True)

    result, _ = await asyncio.gather(
        build_reentry_service(sessions, now).open_reentry(sale.exit_id, request_key="pause-race"),
        pause(),
    )

    if result.kind == "reentry_opened":
        assert await counts(sessions) == (2, 2)
    else:
        assert result.reason is ReentryRefusal.SYSTEM_PAUSED
        assert await counts(sessions) == (1, 1)
    assert (await read_account(sessions)).paused is True


async def test_a_failure_before_commit_leaves_no_case_and_no_cycle(risk_db, now, trace):
    """All of it or none of it: the successor case and its cycle link."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, _, sale = await closed_cycle(sessions, now, trace, feed=feed)
    service = build_reentry_service(sessions, now)
    original = type(service)._opened

    async def explode(self, *arguments, **keywords):
        await original(self, *arguments, **keywords)
        raise RuntimeError("the transaction dies after everything is staged")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(type(service), "_opened", explode)
        with pytest.raises(RuntimeError):
            await service.open_reentry(sale.exit_id, request_key="boom")

    assert await counts(sessions) == (1, 1)
    assert len(await cycles(sessions)) == 1

    # And a later call still succeeds: nothing was consumed by the failure.
    again = await build_reentry_service(sessions, now).open_reentry(
        sale.exit_id, request_key="after-boom"
    )
    assert again.kind == "reentry_opened"
    assert await counts(sessions) == (2, 2)


async def test_no_market_port_is_consulted_at_all(risk_db, now, trace):
    """Nothing here prices or judges anything, so nothing waits on a provider."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, _, sale = await closed_cycle(sessions, now, trace, feed=feed)
    service = build_reentry_service(sessions, now)

    assert not hasattr(service, "markets")
    result = await service.open_reentry(sale.exit_id, request_key="no-market")

    assert result.kind == "reentry_opened"
    assert Decimal(len(await cycles(sessions))) == Decimal("2")
