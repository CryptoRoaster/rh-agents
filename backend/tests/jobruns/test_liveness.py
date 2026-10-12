"""EXIT_JOB_STALE: no BUY in split operation without recent proof that exits work.

The pure judgements first — what one exit run is evidence of, and whether the
latest evidence covers a BUY at an instant — then the two BUY paths that apply
them under the paper account lock: the risk request (before SENTINEL is asked)
and the fill (at the fill instant, whatever was approved before).
"""

import asyncio
import os
from datetime import timedelta

import pytest
from sqlalchemy import select

from src.data.tables import AccountRow, TradeCaseExecutionRow, TradeCaseRiskRequestRow
from src.orchestration.jobruns.models import (
    Coverage,
    EvidenceRow,
    ExitHealth,
    ExitRunFacts,
    JobOutcome,
    PolicyClass,
    SweepFacts,
    assess_exit_run,
    judge_exit_liveness,
)
from tests.casefill.conftest import approved_case, build_fill_service
from tests.entryhalt.conftest import halt, rows
from tests.jobruns.conftest import GATE, exit_run
from tests.riskrequest.conftest import build_service, ready_case

BOTH = frozenset({PolicyClass.NORMAL, PolicyClass.EARLY})


def facts(**overrides):
    values = {
        "lock": "ACQUIRED",
        "stop": "NOTHING_LEFT_TO_DO",
        "errors": (),
        "policies": BOTH,
        "coverage": Coverage(normal=1, early=1),
        "acquisition_enabled": True,
        "acquisition_stop": "COMPLETED",
        "acquisition_shortfall": 0,
        "sweeps": (SweepFacts(), SweepFacts()),
    }
    values.update(overrides)
    return ExitRunFacts(**values)


# ------------------------------------------------------- one exit run's health


def test_a_clean_exit_run_is_evidence_that_exits_work():
    assessed = assess_exit_run(facts())
    assert (assessed.outcome, assessed.ready, assessed.reason) == (JobOutcome.SUCCEEDED, True, None)


def test_an_exit_run_with_nothing_open_is_healthy_when_its_checks_passed():
    assessed = assess_exit_run(
        facts(coverage=Coverage(), acquisition_stop="NOTHING_TO_ACQUIRE", sweeps=())
    )
    assert assessed.ready


@pytest.mark.parametrize(
    ("change", "outcome", "reason"),
    [
        ({"lock": "ALREADY_RUNNING"}, JobOutcome.ALREADY_RUNNING, ExitHealth.ALREADY_RUNNING),
        ({"errors": ("DATABASE_UNAVAILABLE",)}, JobOutcome.TECHNICAL_FAILURE, None),
        ({"lock": None}, JobOutcome.TECHNICAL_FAILURE, ExitHealth.LOCK_NOT_HELD),
        ({"stop": "SYSTEM_STOPPED"}, JobOutcome.REFUSED, ExitHealth.SYSTEM_STOPPED),
        ({"stop": "TIME_BUDGET_REACHED"}, JobOutcome.TIMED_OUT, ExitHealth.TIMED_OUT),
        ({"policies": frozenset()}, JobOutcome.EXIT_COVERAGE_MISSING, ExitHealth.NO_POLICY),
        (
            {"policies": frozenset({PolicyClass.NORMAL})},
            JobOutcome.EXIT_COVERAGE_MISSING,
            ExitHealth.COVERAGE_MISSING,
        ),
        (
            {"coverage": Coverage(normal=1, unknown=1)},
            JobOutcome.EXIT_COVERAGE_MISSING,
            ExitHealth.COVERAGE_MISSING,
        ),
        ({"coverage": None}, JobOutcome.TECHNICAL_FAILURE, ExitHealth.COVERAGE_UNKNOWN),
        ({"acquisition_enabled": False}, JobOutcome.SUCCEEDED, ExitHealth.ACQUISITION_DISABLED),
        ({"acquisition_stop": "PROVIDER_FAILED"}, JobOutcome.SUCCEEDED, None),
        ({"acquisition_shortfall": 1}, JobOutcome.SUCCEEDED, ExitHealth.ACQUISITION_INCOMPLETE),
        ({"sweeps": (SweepFacts(unevaluated=1),)}, JobOutcome.SUCCEEDED, None),
        ({"sweeps": (SweepFacts(plan_stopped=True),)}, JobOutcome.SUCCEEDED, None),
        ({"sweeps": (SweepFacts(unknown=1),)}, JobOutcome.SUCCEEDED, ExitHealth.OUTCOME_UNKNOWN),
        ({"sweeps": (SweepFacts(failed=1),)}, JobOutcome.SUCCEEDED, ExitHealth.ATTEMPT_FAILED),
        ({"sweeps": (SweepFacts(blocked=1),)}, JobOutcome.SUCCEEDED, ExitHealth.BLOCKED),
        (
            {"sweeps": (SweepFacts(timed_out=1),)},
            JobOutcome.SUCCEEDED,
            ExitHealth.ATTEMPT_TIMED_OUT,
        ),
        ({"sweeps": (SweepFacts(deferred=1),)}, JobOutcome.SUCCEEDED, ExitHealth.BACKLOG),
        ({"sweeps": (SweepFacts(mark_unknown=1),)}, JobOutcome.SUCCEEDED, ExitHealth.MARK_UNKNOWN),
    ],
)
def test_an_exit_run_that_did_not_demonstrably_work_is_not_evidence(change, outcome, reason):
    assessed = assess_exit_run(facts(**change))

    assert assessed.ready is False
    assert assessed.outcome is outcome
    if reason is not None:
        assert assessed.reason is reason
    assert assessed.reason is not None


def test_exit_code_zero_alone_is_not_health():
    """A run that ended quietly with a whole policy class unwatched is not healthy."""
    assessed = assess_exit_run(facts(policies=frozenset({PolicyClass.EARLY})))
    assert not assessed.ready


# ------------------------------------------------- the latest evidence, judged


def row(now, *, finished=0, ready=True, policies=("EARLY", "NORMAL"), outcome="SUCCEEDED"):
    done = None if finished is None else now - timedelta(seconds=finished)
    return EvidenceRow(
        outcome="RUNNING" if done is None else outcome,
        started_at=(done or now) - timedelta(seconds=20),
        finished_at=done,
        exit_ready=ready,
        exit_ready_reason=None if ready else "EXIT_JOB_TECHNICAL_FAILURE",
        policy_classes=list(policies),
    )


def judge(rows_, now, policy=None):
    return judge_exit_liveness(rows_, at=now, gate=GATE, policy=policy)


def test_recent_ready_evidence_covers_a_buy(now):
    assert judge([row(now, finished=30)], now, PolicyClass.NORMAL) is None


def test_no_evidence_is_missing(now):
    assert judge([], now) == "EXIT_JOB_MISSING"


def test_old_evidence_is_too_old(now):
    assert judge([row(now, finished=181)], now) == "EXIT_JOB_TOO_OLD"
    assert judge([row(now, finished=180)], now) is None


def test_a_failed_latest_run_is_unhealthy_even_with_a_good_one_before(now):
    assert judge([row(now, finished=10, ready=False), row(now, finished=70)], now) == (
        "EXIT_JOB_UNHEALTHY"
    )


def test_a_run_in_progress_defers_to_the_run_before_it(now):
    running = row(now, finished=None)
    assert judge([running, row(now, finished=60)], now) is None
    assert judge([running, row(now, finished=60, ready=False)], now) == "EXIT_JOB_UNHEALTHY"


def test_a_run_that_never_finished_is_incomplete(now):
    crashed = EvidenceRow(
        outcome="RUNNING",
        started_at=now - timedelta(seconds=GATE.run_max_seconds + 31),
        finished_at=None,
        exit_ready=False,
        exit_ready_reason=None,
        policy_classes=[],
    )
    assert judge([crashed, row(now, finished=100)], now) == "EXIT_JOB_INCOMPLETE"


def test_two_unfinished_runs_are_incomplete(now):
    assert judge([row(now, finished=None), row(now, finished=None)], now) == "EXIT_JOB_INCOMPLETE"


def test_evidence_from_the_future_is_not_trusted(now):
    assert judge([row(now, finished=-5)], now) == "EXIT_JOB_CLOCK_AHEAD"


def test_a_buy_of_a_class_the_exit_job_does_not_close_is_refused(now):
    evidence = [row(now, finished=10, policies=("NORMAL",))]
    assert judge(evidence, now, PolicyClass.NORMAL) is None
    assert judge(evidence, now, PolicyClass.EARLY) == "EXIT_POLICY_NOT_COVERED"


@pytest.mark.parametrize(
    "broken",
    [
        {"policy_classes": "NORMAL"},
        {"policy_classes": None},
        {"finished_at": "yesterday"},
        {"started_at": None, "finished_at": None, "outcome": "RUNNING"},
        {"exit_ready": "true"},
    ],
)
def test_a_record_that_cannot_be_read_as_evidence_is_not_evidence(now, broken):
    values = row(now, finished=10).__dict__ | broken
    assert judge([EvidenceRow(**values)], now, PolicyClass.NORMAL) is not None


# ------------------------------------------------------- the risk request


async def test_a_risk_request_without_exit_evidence_is_refused_and_spends_nothing(
    risk_db, now, trace
):
    _, sessions = risk_db
    service = build_service(sessions, now, exit_liveness=GATE)
    case = await ready_case(service.cases, now, trace)

    refused = await service.request_risk_evaluation(case.id, request_key="no-exits")

    assert (refused.kind, refused.reason.value, refused.detail) == (
        "risk_request_refused",
        "EXIT_JOB_STALE",
        "EXIT_JOB_MISSING",
    )
    assert await rows(sessions, TradeCaseRiskRequestRow) == 0


@pytest.mark.parametrize(
    ("setup", "detail"),
    [
        ({"finished_ago": 600}, "EXIT_JOB_TOO_OLD"),
        ({"finished_ago": 10, "ready": False}, "EXIT_JOB_UNHEALTHY"),
        ({"finished_ago": 10, "policies": ("EARLY",)}, "EXIT_POLICY_NOT_COVERED"),
    ],
)
async def test_a_risk_request_on_stale_or_failed_exit_evidence_is_refused(
    risk_db, now, trace, setup, detail
):
    _, sessions = risk_db
    await exit_run(
        sessions,
        now - timedelta(seconds=setup["finished_ago"]),
        ready=setup.get("ready", True),
        policies=setup.get("policies", ("EARLY", "NORMAL")),
    )
    service = build_service(sessions, now, exit_liveness=GATE)
    case = await ready_case(service.cases, now, trace)

    refused = await service.request_risk_evaluation(case.id, request_key="stale")

    assert (refused.reason.value, refused.detail) == ("EXIT_JOB_STALE", detail)


async def test_a_risk_request_with_live_exits_is_asked(risk_db, now, trace):
    _, sessions = risk_db
    await exit_run(sessions, now - timedelta(seconds=30))
    service = build_service(sessions, now, exit_liveness=GATE)
    case = await ready_case(service.cases, now, trace)

    answered = await service.request_risk_evaluation(case.id, request_key="live")

    assert answered.kind == "risk_request_evaluated", answered


async def test_without_the_gate_nothing_is_consulted(risk_db, now, trace):
    """`--once` without split operation: exactly what it did before."""
    _, sessions = risk_db
    service = build_service(sessions, now)
    case = await ready_case(service.cases, now, trace)

    answered = await service.request_risk_evaluation(case.id, request_key="ungated")

    assert answered.kind == "risk_request_evaluated", answered


async def test_an_entry_halt_still_refuses_first(risk_db, now, trace):
    _, sessions = risk_db
    await exit_run(sessions, now - timedelta(seconds=30))
    service = build_service(sessions, now, exit_liveness=GATE)
    case = await ready_case(service.cases, now, trace)
    await halt(sessions, "manual", now)

    refused = await service.request_risk_evaluation(case.id, request_key="halted")

    assert refused.reason.value == "ENTRY_HALTED"


# ------------------------------------------------------------------- the fill


async def test_an_approval_granted_on_live_exits_fills_while_they_stay_live(risk_db, now, trace):
    _, sessions = risk_db
    await exit_run(sessions, now - timedelta(seconds=30))
    case, approval, feed = await approved_case(sessions, now, trace, key="live")
    assert approval.kind == "risk_request_evaluated", approval

    filled = await build_fill_service(
        sessions, now, feed=feed, exit_liveness=GATE
    ).execute_case_fill(case.id, request_key="live-req")

    assert filled.kind == "paper_fill_recorded", filled


async def test_an_old_approval_does_not_outlive_the_exit_job(risk_db, now, trace):
    """Approved while exits were live; by the fill instant the evidence aged out."""
    _, sessions = risk_db
    await exit_run(sessions, now - timedelta(seconds=170))
    case, approval, feed = await approved_case(sessions, now, trace, key="aging")
    assert approval.kind == "risk_request_evaluated", approval
    later = now + timedelta(seconds=15)

    refused = await build_fill_service(
        sessions, later, feed=feed, exit_liveness=GATE
    ).execute_case_fill(case.id, request_key="aging-req")

    assert (refused.kind, refused.reason.value, refused.detail) == (
        "execution_refused",
        "EXIT_JOB_STALE",
        "EXIT_JOB_TOO_OLD",
    )
    assert await rows(sessions, TradeCaseExecutionRow) == 0


async def test_a_failed_exit_run_after_the_approval_stops_the_fill(risk_db, now, trace):
    _, sessions = risk_db
    await exit_run(sessions, now - timedelta(seconds=60))
    case, approval, feed = await approved_case(sessions, now, trace, key="failed")
    assert approval.kind == "risk_request_evaluated", approval
    await exit_run(sessions, now - timedelta(seconds=1), ready=False)

    refused = await build_fill_service(
        sessions, now, feed=feed, exit_liveness=GATE
    ).execute_case_fill(case.id, request_key="failed-req")

    assert (refused.reason.value, refused.detail) == ("EXIT_JOB_STALE", "EXIT_JOB_UNHEALTHY")
    assert await rows(sessions, TradeCaseExecutionRow) == 0


async def test_a_booked_fill_replays_whatever_the_exit_job_does_since(risk_db, now, trace):
    _, sessions = risk_db
    await exit_run(sessions, now - timedelta(seconds=30))
    case, _, feed = await approved_case(sessions, now, trace, key="replay")
    fills = build_fill_service(sessions, now, feed=feed, exit_liveness=GATE)
    assert (await fills.execute_case_fill(case.id, request_key="replay-req")).kind == (
        "paper_fill_recorded"
    )
    await exit_run(sessions, now, ready=False)

    again = await fills.execute_case_fill(case.id, request_key="replay-req")

    assert again.replayed
    assert await rows(sessions, TradeCaseExecutionRow) == 1


async def test_the_gate_is_judged_again_at_the_execution_boundary(risk_db, now, trace):
    """Evidence valid at the fill instant, aged out by the moment the order is placed."""
    from src.core.clock import FixedClock

    _, sessions = risk_db
    await exit_run(sessions, now - timedelta(seconds=179))
    case, approval, feed = await approved_case(sessions, now, trace, key="edge")
    assert approval.kind == "risk_request_evaluated", approval
    service = build_fill_service(sessions, now, feed=feed, exit_liveness=GATE)
    # The executor reads its own clock at the boundary: two seconds on, the
    # evidence is 181 seconds old.
    service.paper._clock = FixedClock(now + timedelta(seconds=2))

    refused = await service.execute_case_fill(case.id, request_key="edge-req")

    assert (refused.kind, refused.reason.value, refused.detail) == (
        "execution_refused",
        "EXIT_JOB_STALE",
        "EXIT_JOB_TOO_OLD",
    )
    assert await rows(sessions, TradeCaseExecutionRow) == 0


# ------------------------------------------------------------- PostgreSQL

POSTGRES = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required"
)


@POSTGRES
async def test_a_fill_queued_on_the_account_lock_meets_the_exit_verdict_committed_meanwhile(
    risk_db, now, trace
):
    """The exit job records without the account lock; the fill reads after taking it.

    While another transaction holds the paper account, the fill waits; the exit
    job's failed run commits regardless — no second lock order, no wait on a
    fill — and the fill, once it has the lock, judges the evidence as it now
    stands.
    """
    _, sessions = risk_db
    await exit_run(sessions, now - timedelta(seconds=30))
    case, approval, feed = await approved_case(sessions, now, trace, key="queued")
    assert approval.kind == "risk_request_evaluated", approval
    fills = build_fill_service(sessions, now, feed=feed, exit_liveness=GATE)

    async with sessions.begin() as holder:
        await holder.scalar(select(AccountRow).where(AccountRow.id == 1).with_for_update())
        fill = asyncio.create_task(fills.execute_case_fill(case.id, request_key="queued-req"))
        await asyncio.sleep(0.3)
        assert not fill.done()
        # Committed while the account is held: recording never waits on it.
        await asyncio.wait_for(exit_run(sessions, now, ready=False), timeout=5)

    refused = await asyncio.wait_for(fill, timeout=30)
    assert (refused.kind, refused.reason.value, refused.detail) == (
        "execution_refused",
        "EXIT_JOB_STALE",
        "EXIT_JOB_UNHEALTHY",
    )
    assert await rows(sessions, TradeCaseExecutionRow) == 0


@POSTGRES
async def test_concurrent_fills_and_exit_records_do_not_deadlock(risk_db, now, trace):
    _, sessions = risk_db
    await exit_run(sessions, now - timedelta(seconds=30))
    first, _, feed = await approved_case(sessions, now, trace, key="one")
    fills = build_fill_service(sessions, now, feed=feed, exit_liveness=GATE)

    results = await asyncio.wait_for(
        asyncio.gather(
            fills.execute_case_fill(first.id, request_key="one-req"),
            *(exit_run(sessions, now - timedelta(seconds=index)) for index in range(5)),
        ),
        timeout=60,
    )

    assert results[0].kind == "paper_fill_recorded", results[0]
