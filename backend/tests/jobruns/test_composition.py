"""The configured gate reaches the composed BUY paths, and the database guards the pause.

Written against the public surface only (settings, the composed stack, SQL), so
the same file states the defect on a tree without split operation: there the
setting is ignored and the BUY path asks SENTINEL, and `paused` can be turned
off without any audit.
"""

import os
from dataclasses import replace

import pytest
from sqlalchemy import update
from sqlalchemy.exc import DBAPIError

from src.data.tables import AccountRow
from tests.riskrequest.conftest import RecordedMarkets, fresh_snapshot, ready_case, set_account
from tests.runner.conftest import runner_settings, stack_for

POSTGRES = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="the audit trigger is PostgreSQL's"
)


async def test_split_operation_refuses_a_risk_request_without_exit_evidence(risk_db, now, trace):
    _, sessions = risk_db
    stack = stack_for(sessions, runner_settings(paper_entry_requires_exit_job=True), now)
    # The composed service, reading the market the case was made ready on.
    risk = replace(stack.risk, markets=RecordedMarkets(fresh_snapshot(now)))
    case = await ready_case(risk.cases, now, trace)

    answer = await risk.request_risk_evaluation(case.id, request_key="split")

    assert answer.kind == "risk_request_refused", answer
    assert (answer.reason.value, answer.detail) == ("EXIT_JOB_STALE", "EXIT_JOB_MISSING")


async def test_the_fill_path_is_composed_with_the_same_gate(risk_db, now):
    _, sessions = risk_db
    stack = stack_for(sessions, runner_settings(paper_entry_requires_exit_job=True), now)

    assert stack.fills.exit_liveness is not None
    assert stack.fills.exit_liveness == stack.risk.exit_liveness


def test_the_gate_cannot_demand_evidence_younger_than_one_exit_run():
    with pytest.raises(ValueError, match="PAPER_EXIT_LIVENESS_MAX_AGE_SECONDS"):
        runner_settings(
            paper_entry_requires_exit_job=True,
            paper_exit_run_max_seconds=120,
            paper_exit_liveness_max_age_seconds=60,
        )


@POSTGRES
async def test_a_pause_cannot_be_lifted_without_its_audit_event(risk_db):
    _, sessions = risk_db
    await set_account(sessions, paused=True)

    with pytest.raises(DBAPIError, match="system pause cleared without an audit event"):
        async with sessions.begin() as session:
            await session.execute(update(AccountRow).values(paused=False))
