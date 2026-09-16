"""A second trading cycle on one market, and every reason there is not one.

The production path runs throughout: the real workflow service against a real
database, the real risk request, the real case fill, `src.risk.engine.evaluate`,
`PaperExecutor`, the real ledger postings and the real exit service. Both cycles
are real; nothing is inserted to stand in for one.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from src.data.tables import (
    PositionRow,
    TradeCaseExecutionRow,
    TradeCaseExitRow,
)
from src.orchestration.reentry.models import ReentryRefusal
from src.orchestration.workflow.models import TradeCaseStatus
from tests.reentry.conftest import (
    ZERO,
    build_exit_service,
    build_reentry_service,
    closed_cycle,
    cycles,
    entered,
    holding,
    market_feed,
    money,
    position_of,
    set_account,
)

# ------------------------------------------------------- the whole chain


async def test_a_full_second_cycle_runs_through_every_ordinary_check(risk_db, now, trace):
    """Entry 1, exit 1, explicit re-entry, new case, new evidence, entry 2, exit 2.

    Nothing is carried over: the successor case starts where every case starts
    and proves itself from nothing.
    """
    _, sessions = risk_db
    feed = market_feed(now)
    first_case, first_entry, position, first_exit = await closed_cycle(
        sessions, now, trace, feed=feed
    )

    reentry = await build_reentry_service(sessions, now).open_reentry(
        first_exit.exit_id, request_key="again"
    )

    assert reentry.kind == "reentry_opened", getattr(reentry, "reason", None)
    assert reentry.sequence == 2
    assert reentry.predecessor_exit_id == first_exit.exit_id
    assert reentry.predecessor_trade_case_id == first_case.id
    assert reentry.authorizes_execution is False
    # The workflow's own starting state, reached through the workflow.
    assert reentry.trade_case_status in ("DISCOVERED", "EVIDENCE_PENDING")
    assert reentry.trade_case_id != first_case.id

    # The second cycle now runs the ordinary path: evidence, risk request, fill.
    second_case, second_entry, second_position = await second_cycle(
        sessions, now, uuid4(), reentry.trade_case_id, feed
    )
    assert second_position.quantity > 0
    assert second_position.cycle_id == reentry.cycle_id

    sale = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        second_position.id, request_key="cycle-two-exit"
    )
    assert sale.kind == "paper_exit_recorded", getattr(sale, "detail", None)

    # Two cycles, separate and consistent throughout.
    both = await cycles(sessions)
    assert [item.sequence for item in both] == [1, 2]
    assert both[1].predecessor_exit_id == first_exit.exit_id
    assert both[0].trade_case_id == first_case.id
    assert both[1].trade_case_id == second_case
    async with sessions() as session:
        entries = (await session.scalars(select(TradeCaseExecutionRow))).all()
        sales = (await session.scalars(select(TradeCaseExitRow))).all()
    assert {item.cycle_id for item in entries} == {both[0].cycle_id, both[1].cycle_id}
    assert {item.cycle_id for item in sales} == {both[0].cycle_id, both[1].cycle_id}
    # The second exit found its own entry, not the first one.
    second_sale = next(item for item in sales if item.cycle_id == both[1].cycle_id)
    assert second_sale.case_execution_id == second_entry
    assert second_sale.case_execution_id != first_entry.case_execution_id


async def second_cycle(sessions, now, trace, case_id, feed):
    """Carry an already-opened successor case through the ordinary path."""
    from tests.casefill.conftest import build_fill_service
    from tests.riskrequest.conftest import build_service, evidence_for

    risk = build_service(sessions, now, feed=feed)
    await evidence_for(risk.cases, case_id, now)
    approval = await risk.request_risk_evaluation(case_id, request_key="cycle-two-req")
    assert approval.kind == "risk_request_evaluated", getattr(approval, "reason", None)
    fill = await build_fill_service(sessions, now, feed=feed).execute_case_fill(
        case_id, request_key="cycle-two-req"
    )
    assert fill.kind == "paper_fill_recorded", getattr(fill, "detail", None)
    return case_id, fill.case_execution_id, await position_of(sessions)


async def test_the_first_cycle_s_records_replay_unchanged_afterwards(risk_db, now, trace):
    """History is history: reopening a market rewrites none of it."""
    _, sessions = risk_db
    feed = market_feed(now)
    first_case, first_entry, position, first_exit = await closed_cycle(
        sessions, now, trace, feed=feed
    )
    reentry = await build_reentry_service(sessions, now).open_reentry(
        first_exit.exit_id, request_key="again"
    )
    await second_cycle(sessions, now, uuid4(), reentry.trade_case_id, feed)

    from tests.casefill.conftest import build_fill_service

    entry_again = await build_fill_service(sessions, now, feed=feed).execute_case_fill(
        first_case.id, request_key="cycle-one-req"
    )
    assert entry_again.replayed is True
    assert entry_again.execution_id == first_entry.execution_id
    assert entry_again.recheck_decision_id == first_entry.recheck_decision_id

    exit_again = await build_exit_service(sessions, now, feed=feed).execute_position_exit(
        position.id, request_key="cycle-one-exit"
    )
    assert exit_again.replayed is True
    assert exit_again.execution_id == first_exit.execution_id
    assert money(exit_again.realized_pnl_usd) == money(first_exit.realized_pnl_usd)


# ------------------------------------------------------- preconditions


async def test_an_open_holding_blocks_a_re_entry(risk_db, now, trace):
    """A cycle that still owns something has not ended."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position, sale = await closed_cycle(sessions, now, trace, feed=feed)
    async with sessions.begin() as session:
        await session.execute(
            update(PositionRow).where(PositionRow.id == position.id).values(quantity=Decimal("2"))
        )

    result = await build_reentry_service(sessions, now).open_reentry(
        sale.exit_id, request_key="open"
    )

    assert result.reason is ReentryRefusal.POSITION_STILL_OPEN
    assert len(await cycles(sessions)) == 1


async def test_a_lingering_cost_basis_blocks_a_re_entry(risk_db, now, trace):
    """Quantity nil is not enough: an unreleased basis is an unclosed cycle."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position, sale = await closed_cycle(sessions, now, trace, feed=feed)
    async with sessions.begin() as session:
        await session.execute(
            update(PositionRow)
            .where(PositionRow.id == position.id)
            .values(cost_basis_usd=Decimal("5"))
        )

    result = await build_reentry_service(sessions, now).open_reentry(
        sale.exit_id, request_key="basis"
    )

    assert result.reason is ReentryRefusal.POSITION_STILL_OPEN
    assert len(await cycles(sessions)) == 1


async def test_a_closed_position_row_alone_is_not_an_exit(risk_db, now, trace):
    """There is no exit to succeed, so there is nothing to open."""
    _, sessions = risk_db
    feed = market_feed(now)
    await entered(sessions, now, trace, feed=feed)

    result = await build_reentry_service(sessions, now).open_reentry(uuid4(), request_key="ghost")

    assert result.reason is ReentryRefusal.PREDECESSOR_EXIT_NOT_FOUND
    assert len(await cycles(sessions)) == 1


async def test_an_exit_whose_records_disagree_is_refused(risk_db, now, trace):
    """One of the records is wrong, and a new cycle is not where that is settled."""
    from tests.paperexit.conftest import market_for
    from tests.riskrequest.conftest import ready_case

    _, sessions = risk_db
    feed = market_feed(now)
    _, _, _, sale = await closed_cycle(sessions, now, trace, feed=feed)

    # A real, entirely unrelated case, so the reference is valid and still wrong.
    service = build_reentry_service(sessions, now)
    stranger = await ready_case(
        service.cases,
        now,
        uuid4(),
        key="stranger",
        identity=market_for(token="c7" * 20, pool="d8" * 20),
    )
    async with sessions.begin() as session:
        await session.execute(
            update(TradeCaseExitRow)
            .where(TradeCaseExitRow.exit_id == sale.exit_id)
            .values(trade_case_id=stranger.id)
        )

    result = await service.open_reentry(sale.exit_id, request_key="wrong")

    assert result.reason is ReentryRefusal.PREDECESSOR_MISMATCH
    assert len(await cycles(sessions)) == 1


async def test_a_cycle_that_never_executed_cannot_be_succeeded(risk_db, now, trace):
    """A rejection, an abandoned entry and a lapsed case are not completed trades."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, _, sale = await closed_cycle(sessions, now, trace, feed=feed)
    async with sessions.begin() as session:
        from src.data.tables import TradeCaseRow

        await session.execute(
            update(TradeCaseRow)
            .where(TradeCaseRow.id == sale.trade_case_id)
            .values(status=TradeCaseStatus.EXPIRED.value)
        )

    result = await build_reentry_service(sessions, now).open_reentry(
        sale.exit_id, request_key="lapsed"
    )

    assert result.reason is ReentryRefusal.PREDECESSOR_NOT_EXECUTED
    assert result.detail == "EXPIRED"
    assert len(await cycles(sessions)) == 1


# ------------------------------------------------------- succession


async def test_one_completed_exit_gets_exactly_one_successor(risk_db, now, trace):
    """A different key is not a second permission."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, _, sale = await closed_cycle(sessions, now, trace, feed=feed)
    service = build_reentry_service(sessions, now)
    first = await service.open_reentry(sale.exit_id, request_key="one")
    assert first.kind == "reentry_opened"

    second = await service.open_reentry(sale.exit_id, request_key="two")

    assert second.reason is ReentryRefusal.SUCCESSOR_ALREADY_EXISTS
    assert len(await cycles(sessions)) == 2


async def test_the_same_key_replays_the_same_successor(risk_db, now, trace):
    """One order, one cycle. A retry finds it rather than opening another."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, _, sale = await closed_cycle(sessions, now, trace, feed=feed)
    service = build_reentry_service(sessions, now)
    first = await service.open_reentry(sale.exit_id, request_key="same")

    again = await service.open_reentry(sale.exit_id, request_key="same")

    assert again.kind == "reentry_opened"
    assert again.replayed is True
    assert again.trade_case_id == first.trade_case_id
    assert again.cycle_id == first.cycle_id
    assert len(await cycles(sessions)) == 2


async def test_a_key_that_names_another_predecessor_is_refused(risk_db, now, trace):
    """Two callers must not end up believing they own one cycle."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, _, sale = await closed_cycle(sessions, now, trace, feed=feed)
    service = build_reentry_service(sessions, now)
    assert (await service.open_reentry(sale.exit_id, request_key="taken")).kind == (
        "reentry_opened"
    )

    result = await service.open_reentry(uuid4(), request_key="taken")

    assert result.reason is ReentryRefusal.PREDECESSOR_EXIT_NOT_FOUND
    assert len(await cycles(sessions)) == 2


async def test_a_refused_successor_is_not_retried_under_a_new_key(risk_db, now, trace):
    """A risk rejection in cycle two does not return the market to the queue."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, _, sale = await closed_cycle(sessions, now, trace, feed=feed)
    service = build_reentry_service(sessions, now)
    opened = await service.open_reentry(sale.exit_id, request_key="first-try")
    assert opened.kind == "reentry_opened"

    # Cycle two is rejected on its merits and ends terminal.
    from tests.riskrequest.conftest import build_service, evidence_for

    risk = build_service(sessions, now, feed=feed, limits=strict())
    await evidence_for(risk.cases, opened.trade_case_id, now)
    verdict = await risk.request_risk_evaluation(opened.trade_case_id, request_key="cycle-two-req")
    assert verdict.kind == "risk_request_evaluated"
    assert verdict.outcome.value in ("REJECT", "PAUSE_SYSTEM")

    again = await service.open_reentry(sale.exit_id, request_key="second-try")

    assert again.reason is ReentryRefusal.SUCCESSOR_ALREADY_EXISTS
    assert len(await cycles(sessions)) == 2


def strict():
    from decimal import Decimal as D

    from src.core.models import RiskLimits

    return RiskLimits(min_liquidity_usd=D("999999999"))


# ------------------------------------------------------- stops


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"pause": None}, ReentryRefusal.SYSTEM_STOP_UNREADABLE),
        ({"kill_switch": True}, ReentryRefusal.KILL_SWITCH_ENGAGED),
        ({"trading_mode": "OBSERVE"}, ReentryRefusal.KILL_SWITCH_ENGAGED),
    ],
)
async def test_every_stop_blocks_a_re_entry(risk_db, now, trace, overrides, expected):
    """No special rights. A stop that does not stop a new trade is not a stop."""
    from src.core.models import TradingMode

    _, sessions = risk_db
    feed = market_feed(now)
    _, _, _, sale = await closed_cycle(sessions, now, trace, feed=feed)
    if overrides.get("trading_mode") == "OBSERVE":
        overrides = {"trading_mode": TradingMode.OBSERVE}

    result = await build_reentry_service(sessions, now, **overrides).open_reentry(
        sale.exit_id, request_key="stopped"
    )

    assert result.reason is expected
    assert len(await cycles(sessions)) == 1


async def test_a_paused_account_blocks_a_re_entry(risk_db, now, trace):
    """Read from the locked row, like every other reader of this stop."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, _, sale = await closed_cycle(sessions, now, trace, feed=feed)
    await set_account(sessions, paused=True)

    result = await build_reentry_service(sessions, now).open_reentry(
        sale.exit_id, request_key="paused"
    )

    assert result.reason is ReentryRefusal.SYSTEM_PAUSED
    assert len(await cycles(sessions)) == 1

    await set_account(sessions, paused=False)
    allowed = await build_reentry_service(sessions, now).open_reentry(
        sale.exit_id, request_key="unpaused"
    )
    assert allowed.kind == "reentry_opened"


# ------------------------------------------------------- intake


async def test_ordinary_intake_never_opens_the_successor(risk_db, now, trace):
    """Only the explicit contract may, and only for a completed cycle."""
    from tests.casefill.conftest import candidate_for
    from tests.commander.conftest import intake_service
    from tests.reentry.conftest import recorded_snapshot

    _, sessions = risk_db
    feed = market_feed(now)
    _, _, _, sale = await closed_cycle(sessions, now, trace, feed=feed)
    snapshot = recorded_snapshot(now, age=timedelta(seconds=5))
    intake = intake_service(
        sessions,
        now,
        candidates=(candidate_for(snapshot),),
        snapshots={snapshot.pair.pair_id: snapshot},
    )

    # Barred after the completed cycle.
    first = await intake.run_cycle()
    assert first.opened == ()
    assert [reason for _, reason in first.refused] == ["POSITION_OPENED_FOR_MARKET"]

    opened = await build_reentry_service(sessions, now).open_reentry(
        sale.exit_id, request_key="only-this-way"
    )
    assert opened.kind == "reentry_opened"

    # And still barred while the successor is live: no parallel case.
    second = await intake.run_cycle()
    assert second.opened == ()
    assert [reason for _, reason in second.refused] == ["ACTIVE_CASE_EXISTS"]
    async with sessions() as session:
        from src.data.tables import TradeCaseRow

        assert await session.scalar(select(func.count()).select_from(TradeCaseRow)) == 2


async def test_an_expired_successor_does_not_unbar_the_market(risk_db, now, trace):
    """An executed cycle stays spoken for whatever ends last."""
    from tests.casefill.conftest import candidate_for
    from tests.commander.conftest import intake_service
    from tests.reentry.conftest import recorded_snapshot

    _, sessions = risk_db
    feed = market_feed(now)
    _, _, _, sale = await closed_cycle(sessions, now, trace, feed=feed)
    opened = await build_reentry_service(sessions, now).open_reentry(
        sale.exit_id, request_key="lapses"
    )
    async with sessions.begin() as session:
        from src.data.tables import TradeCaseRow

        await session.execute(
            update(TradeCaseRow)
            .where(TradeCaseRow.id == opened.trade_case_id)
            .values(status=TradeCaseStatus.EXPIRED.value)
        )

    snapshot = recorded_snapshot(now, age=timedelta(seconds=5))
    intake = intake_service(
        sessions,
        now,
        candidates=(candidate_for(snapshot),),
        snapshots={snapshot.pair.pair_id: snapshot},
    )
    outcome = await intake.run_cycle()

    assert outcome.opened == ()
    assert [reason for _, reason in outcome.refused] == ["POSITION_OPENED_FOR_MARKET"]


# ------------------------------------------------------- no inheritance


async def test_the_old_approval_and_evidence_do_not_carry_over(risk_db, now, trace):
    """The successor case starts from nothing and has to prove itself."""
    _, sessions = risk_db
    feed = market_feed(now)
    first_case, _, _, sale = await closed_cycle(sessions, now, trace, feed=feed)
    opened = await build_reentry_service(sessions, now).open_reentry(
        sale.exit_id, request_key="fresh"
    )

    service = build_reentry_service(sessions, now)
    case = await service.cases.get_trade_case(opened.trade_case_id)
    assert case.status is not TradeCaseStatus.RISK_APPROVED
    assert case.risk_input_digest is None
    assert await service.cases.evidence(opened.trade_case_id) != []

    # No binding and no risk request travelled with it.
    async with sessions() as session:
        from src.data.tables import TradeCaseRiskBindingRow, TradeCaseRiskRequestRow

        bindings = (
            await session.scalars(
                select(TradeCaseRiskBindingRow).where(
                    TradeCaseRiskBindingRow.trade_case_id == opened.trade_case_id
                )
            )
        ).all()
        requests = (
            await session.scalars(
                select(TradeCaseRiskRequestRow).where(
                    TradeCaseRiskRequestRow.trade_case_id == opened.trade_case_id
                )
            )
        ).all()
    assert bindings == []
    assert requests == []

    # And a fill attempt on the successor finds no approval to spend.
    from tests.casefill.conftest import build_fill_service

    attempt = await build_fill_service(sessions, now, feed=feed).execute_case_fill(
        opened.trade_case_id, request_key="no-approval"
    )
    assert attempt.kind == "execution_refused"
    assert attempt.reason.value == "REQUEST_NOT_FOUND"


async def test_the_position_row_names_the_cycle_that_owns_it(risk_db, now, trace):
    """One row per asset, reused — and never ambiguous about whose it is."""
    _, sessions = risk_db
    feed = market_feed(now)
    _, _, position, sale = await closed_cycle(sessions, now, trace, feed=feed)
    first = await holding(sessions, position.asset_id)
    assert first.quantity == ZERO
    assert first.cycle_id is not None

    opened = await build_reentry_service(sessions, now).open_reentry(
        sale.exit_id, request_key="owned"
    )
    await second_cycle(sessions, now, uuid4(), opened.trade_case_id, feed)

    after = await holding(sessions, position.asset_id)
    assert after.id == first.id
    assert after.cycle_id == opened.cycle_id
    assert after.cycle_id != first.cycle_id
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PositionRow)) == 1
