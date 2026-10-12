"""PAPER_EARLY_ONLY: a bounded PAPER run that may only ever enter early.

Off by default, and then nothing changes. On, the normal intake and its
promotion refresh are skipped, the run works on early cases only, and a risk
request for any other case is refused before SENTINEL is asked. The early
intake, both exit sweeps and the exit-job liveness gate are exactly what they
were. No provider is called anywhere in this file.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from src.data.tables import TradeCaseRow, TradeCaseTaskRow
from src.orchestration.strategy.early import EARLY_ENTRY_V1, PRE_VECTOR_EARLY_ENTRY_V1
from tests.early.test_candidates import watching
from tests.early.test_runner import early_settings
from tests.entryhalt.conftest import rows
from tests.riskrequest.conftest import build_service, ready_case
from tests.runner.conftest import cases_in, run, runner_settings, stack_for
from tests.scout.test_promotion import NOW, promotable


def early_only(**overrides):
    return early_settings(**{"paper_early_only": True, **overrides})


async def snapshot(sessions, trade_case_id):
    """Everything about one case a run could change: its row and its tasks."""
    async with sessions() as session:
        row = await session.get(TradeCaseRow, trade_case_id)
        tasks = (
            await session.scalars(
                select(TradeCaseTaskRow)
                .where(TradeCaseTaskRow.trade_case_id == trade_case_id)
                .order_by(TradeCaseTaskRow.task_id)
            )
        ).all()
        return (
            row.status,
            row.revision,
            row.updated_at,
            row.reason_code,
            tuple((task.task_id, task.status, task.reason_code) for task in tasks),
        )


# ------------------------------------------------------------------ settings


def test_off_by_default():
    assert runner_settings().paper_early_only is False
    assert early_settings().paper_early_only is False


def test_early_only_requires_the_early_strategy():
    with pytest.raises(
        ValueError, match="PAPER_EARLY_ONLY requires PRE_VECTOR_EARLY_ENTRY_ENABLED"
    ):
        runner_settings(paper_early_only=True)
    # Which in turn brings the scout, the funding graph and the early exit.
    assert early_only().pre_vector_early_entry_enabled is True


# ------------------------------------------------------------------ intake


async def test_default_mode_still_opens_a_normal_case(risk_db):
    _, sessions = risk_db
    await promotable(sessions, 0)

    summary = await run(sessions, early_settings(), NOW)

    assert summary.cases_opened == 1
    assert summary.promotion is not None
    [case] = await cases_in(sessions)
    assert case.strategy_policy_id is None


async def test_early_only_opens_no_normal_case(risk_db):
    _, sessions = risk_db
    await promotable(sessions, 0)

    summary = await run(sessions, early_only(), NOW)

    assert summary.cases_opened == 0
    # The normal promotion refresh is part of the normal intake and is skipped too.
    assert summary.promotion is None
    assert await cases_in(sessions) == []


async def test_early_only_still_opens_an_early_case(risk_db):
    _, sessions = risk_db
    await promotable(sessions, 0)
    watch = await watching(sessions, 1)

    summary = await run(sessions, early_only(), NOW)

    assert summary.cases_opened == 0
    assert summary.early is not None
    assert summary.early.cases_opened == 1
    [case] = await cases_in(sessions)
    assert case.market_key == watch.pair_id
    assert case.strategy_policy_id == PRE_VECTOR_EARLY_ENTRY_V1


async def test_early_only_opens_at_most_one_early_case_per_run(risk_db):
    _, sessions = risk_db
    await watching(sessions, 0, first_seen=NOW - timedelta(minutes=20))
    await watching(sessions, 1, first_seen=NOW - timedelta(minutes=10))

    summary = await run(sessions, early_only(paper_runner_max_new_cases=5), NOW)

    assert summary.early.cases_opened == EARLY_ENTRY_V1.max_entries_per_run == 1
    assert len(await cases_in(sessions)) == 1


# ------------------------------------------------------------------ existing cases


async def test_an_existing_normal_case_is_left_alone_and_takes_no_budget(risk_db):
    _, sessions = risk_db
    await promotable(sessions, 0)
    await run(sessions, early_settings(), NOW)
    [normal] = await cases_in(sessions)
    assert normal.strategy_policy_id is None
    before = await snapshot(sessions, normal.id)
    later = NOW + timedelta(minutes=1)
    await watching(sessions, 1, at=later)

    # One case of budget: the older normal case must not take it.
    summary = await run(
        sessions,
        early_only(
            paper_runner_max_candidates=1, paper_runner_max_new_cases=1, paper_runner_max_cases=1
        ),
        later,
    )

    assert await snapshot(sessions, normal.id) == before
    worked = {item.trade_case_id for item in summary.cases}
    assert normal.id not in worked
    early = [case for case in await cases_in(sessions) if case.id != normal.id]
    assert [case.strategy_policy_id for case in early] == [PRE_VECTOR_EARLY_ENTRY_V1]
    assert summary.early.cases_opened == 1
    [early_case] = early

    # The next run opens nothing new: the one slot must go to the early case
    # an earlier run opened, never to the older normal case.
    again = await run(
        sessions,
        early_only(
            paper_runner_max_candidates=1, paper_runner_max_new_cases=1, paper_runner_max_cases=1
        ),
        later,
    )
    worked = {item.trade_case_id for item in again.cases}
    assert early_case.id in worked
    assert normal.id not in worked
    assert await snapshot(sessions, normal.id) == before


async def test_default_mode_still_works_an_existing_normal_case(risk_db):
    _, sessions = risk_db
    await promotable(sessions, 0)
    await run(sessions, early_settings(), NOW)
    [normal] = await cases_in(sessions)

    summary = await run(sessions, early_settings(), NOW + timedelta(minutes=1))

    assert normal.id in {item.trade_case_id for item in summary.cases}


# ------------------------------------------------------------------ BUY boundary


async def test_a_normal_risk_request_is_refused_in_early_only(risk_db, now, trace):
    _, sessions = risk_db
    service = build_service(sessions, now, early_only=True)
    case = await ready_case(service.cases, now, trace)
    assert case.strategy_policy_id is None

    refused = await service.request_risk_evaluation(case.id, request_key="early-only-req")

    assert refused.kind == "risk_request_refused"
    assert refused.reason.value == "NORMAL_ENTRY_DISABLED"
    # Refused before SENTINEL: nothing recorded, no verdict spent.
    from src.data.tables import TradeCaseRiskRequestRow

    assert await rows(sessions, TradeCaseRiskRequestRow) == 0


async def test_the_same_normal_request_is_unchanged_by_default(risk_db, now, trace):
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)

    outcome = await service.request_risk_evaluation(case.id, request_key="default-req")

    assert getattr(outcome, "reason", None) is None or outcome.reason.value != (
        "NORMAL_ENTRY_DISABLED"
    )


async def test_the_composed_risk_request_carries_the_switch(risk_db):
    _, sessions = risk_db
    assert stack_for(sessions, early_settings(), NOW).risk.early_only is False
    assert stack_for(sessions, early_only(), NOW).risk.early_only is True


# ------------------------------------------------------------------ exits and liveness


async def test_both_exit_sweeps_stay_composed_in_early_only(risk_db):
    _, sessions = risk_db
    stack = stack_for(
        sessions,
        early_only(
            paper_auto_exit_enabled=True,
            paper_exit_stop_loss_bps=2000,
            paper_exit_take_profit_bps=5000,
            paper_exit_max_holding_minutes=600,
        ),
        NOW,
    )
    assert stack.exits is not None
    assert stack.early_exits is not None

    summary = await run(sessions, early_only(), NOW)
    assert summary.early_exits is not None


async def test_the_liveness_gate_is_not_bypassed(risk_db):
    _, sessions = risk_db
    stack = stack_for(sessions, early_only(paper_entry_requires_exit_job=True), NOW)
    assert stack.exit_liveness is not None
    assert stack.risk.exit_liveness is stack.exit_liveness
