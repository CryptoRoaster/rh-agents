"""S6: how a fresh early case reaches risk — replayed through the real bounded run.

The question is chronological. EARLY publishes a setup that lives ten minutes;
PULSE may only confirm it on a market observation recorded *after* the setup
was published. A run observes the market before its specialists work, so does
a single `--entries-once` ever get a new early case past PULSE, or does it take
a second pass inside the setup's lifetime?

Everything between the recorded facts and the verdict is production code: the
real composition, worker runtime, EARLY, PULSE, ANCHOR, risk request, SENTINEL,
fill and ledger. Substituted only at the outside edge: the model (ORBIT), the
hourly history series, the quote source. The ATLAS evidence carrying the
chain-side creation time is recorded as fixture evidence, because the funding
source is composed only from a live RPC contract source — no configuration
can produce one in a test. Every market fact is synthetic.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from src.core.models import AgentRole
from src.data.tables import ExecutionRow, TradeCaseEvidenceRow, WorkerTaskAttemptRow
from src.runner.composition import RunnerPorts
from src.runner.models import ExitCode
from tests.early.conftest import open_early_case, record_early_onchain, young_history
from tests.early.test_runner import early_settings
from tests.runner.conftest import record_market, run
from tests.runner.specialists import ScriptedSpecialists
from tests.runner.test_end_to_end import record_payment_asset
from tests.vector.conftest import StubHistory

SPOT = Decimal("1.00")


def replay_settings(**overrides):
    values: dict[str, object] = {
        "paper_early_only": True,
        "orbit_worker_enabled": True,
        "reasoning_provider": "anthropic",
        "pulse_worker_enabled": True,
        "anchor_worker_enabled": True,
        "evm_runtime_enabled": True,
        "paper_runner_max_candidates": 1,
    }
    return early_settings(**{**values, **overrides})


def replay_ports(now, *, atlas=False):
    from tests.anchor.conftest import source as quote_source

    extra: dict[str, object] = {}
    if atlas:
        from tests.atlas.conftest import (
            StubContracts,
            StubHolders,
            StubOrigins,
            chain_snapshot,
            contract_facts,
            origin_facts,
        )
        from tests.runner.test_end_to_end import dispersed_holders

        extra = {
            "onchain": StubContracts(chain_snapshot(now), contract_facts()),
            "holders": StubHolders(dispersed_holders(now)),
            "origins": StubOrigins(origin_facts()),
        }
    return RunnerPorts(
        reasoning=ScriptedSpecialists(),
        history=StubHistory(young_history(now, bars=3)),
        quotes=quote_source(now, reference_price=SPOT),
        **extra,  # type: ignore[arg-type]
    )


async def staged_case(sessions, now, trace):
    """A young early case the way the early intake leaves it, with ATLAS in hand."""
    from src.core.clock import FixedClock
    from src.orchestration.workflow.service import TradeCaseService

    await record_market(sessions, now, price=SPOT)
    await record_payment_asset(sessions, now)
    cases = TradeCaseService(sessions, clock=FixedClock(now))
    trade_case = await open_early_case(cases, sessions, now, trace)
    await record_early_onchain(cases, trade_case, now, created_ago=timedelta(hours=2))
    return trade_case


async def attempts(sessions, role: AgentRole, trade_case_id):
    async with sessions() as session:
        rows = (
            await session.scalars(
                select(WorkerTaskAttemptRow)
                .where(
                    WorkerTaskAttemptRow.role == role.value,
                    WorkerTaskAttemptRow.trade_case_id == trade_case_id,
                )
                .order_by(WorkerTaskAttemptRow.started_at)
            )
        ).all()
    return rows


async def produced(sessions, trade_case_id) -> set[str]:
    async with sessions() as session:
        rows = (
            await session.scalars(
                select(TradeCaseEvidenceRow).where(
                    TradeCaseEvidenceRow.trade_case_id == trade_case_id
                )
            )
        ).all()
    return {f"{row.producer_role}:{row.evidence_type}" for row in rows}


async def fills(sessions):
    async with sessions() as session:
        return (await session.scalars(select(ExecutionRow))).all()


# PULSE re-checks a waiting watch on its workflow wait cadence (90 s); the EARLY setup
# lives ten minutes. A second pass can only confirm inside that window.
PULSE_POLL = timedelta(seconds=90)
SETUP_LIFETIME = timedelta(minutes=10)


async def first_pass(sessions, now, trace):
    trade_case = await staged_case(sessions, now, trace)
    summary = await run(sessions, replay_settings(), now, ports=replay_ports(now))
    return trade_case, summary


async def second_pass(sessions, later, *, observe=True):
    """The next pass. `observe`: the market was observed again before it, as the
    run's own acquisition does at its start — the only post-setup reading."""
    if observe:
        await record_market(sessions, later, price=SPOT, label="later")
        await record_payment_asset(sessions, later, label="quote-later")
    return await run(
        sessions,
        replay_settings(atlas_worker_enabled=True),
        later,
        ports=replay_ports(later, atlas=True),
    )


def reasons(rows):
    return [(row.outcome, row.reason_code) for row in rows]


async def test_a_single_run_carries_a_fresh_early_case_only_as_far_as_its_trigger(
    risk_db, now, trace
):
    """A: one `--entries-once`. ORBIT and EARLY answer; PULSE cannot confirm."""
    _, sessions = risk_db
    trade_case, summary = await first_pass(sessions, now, trace)

    assert summary.exit_code is ExitCode.COMPLETED, summary
    [progress] = summary.cases
    assert (progress.status, progress.reason_code) == (
        "READY_FOR_TRIGGER",
        "WAITING_FOR_CURRENT_TRIGGER",
    )
    assert summary.fills == 0 and summary.risk_requests == 0
    assert reasons(await attempts(sessions, AgentRole.EARLY, trade_case.id)) == [
        ("SUCCEEDED", "RESULT_ACCEPTED")
    ]
    # PULSE was claimed before the setup existed in this pass and waits its
    # poll interval; nothing in the same pass records a later observation.
    assert reasons(await attempts(sessions, AgentRole.PULSE, trade_case.id)) == [
        ("WAITING", "NO_CURRENT_SETUP")
    ]
    assert await attempts(sessions, AgentRole.ANCHOR, trade_case.id) == []
    assert "PULSE:TRIGGER_EVIDENCE" not in await produced(sessions, trade_case.id)
    assert await fills(sessions) == []


@pytest.mark.parametrize("gap", [PULSE_POLL + timedelta(seconds=10), timedelta(minutes=5)])
async def test_a_second_run_inside_the_setup_window_reaches_risk_and_fills(
    risk_db, now, trace, gap
):
    """B and D: a later observation, then a second pass inside the window.

    PULSE confirms on the post-setup reading, ANCHOR proves capacity, the
    stale ATLAS holder facts are refreshed by the run's own source refresh,
    and SENTINEL approves the ten-dollar early entry (a PAPER fill in the test
    ledger).
    """
    _, sessions = risk_db
    trade_case, _ = await first_pass(sessions, now, trace)

    summary = await second_pass(sessions, now + gap)

    [progress] = summary.cases
    assert (progress.status, progress.risk_outcome) == ("EXECUTED", "APPROVE"), progress
    assert [item.origin for item in progress.refreshes] == ["ATLAS_ONCHAIN_EVIDENCE"]
    assert summary.fills == 1
    assert reasons(await attempts(sessions, AgentRole.PULSE, trade_case.id)) == [
        ("WAITING", "NO_CURRENT_SETUP"),
        ("SUCCEEDED", "RESULT_ACCEPTED"),
    ]
    assert "PULSE:TRIGGER_EVIDENCE" in await produced(sessions, trade_case.id)
    assert len(await fills(sessions)) == 1
    assert progress.execution_id is not None


async def test_a_second_run_before_the_poll_interval_finds_nothing_due(risk_db, now, trace):
    """Too soon: PULSE's watch is not due yet, so nothing changes."""
    _, sessions = risk_db
    trade_case, _ = await first_pass(sessions, now, trace)

    summary = await second_pass(sessions, now + timedelta(seconds=20))

    [progress] = summary.cases
    assert progress.status == "READY_FOR_TRIGGER"
    assert summary.fills == 0
    assert len(await attempts(sessions, AgentRole.PULSE, trade_case.id)) == 1


async def test_without_an_observation_after_the_setup_pulse_never_confirms(risk_db, now, trace):
    """C: a second pass that observed nothing new. The reading predates the setup."""
    _, sessions = risk_db
    trade_case, _ = await first_pass(sessions, now, trace)

    summary = await second_pass(sessions, now + timedelta(minutes=2), observe=False)

    [progress] = summary.cases
    assert progress.status == "READY_FOR_TRIGGER"
    assert summary.fills == 0
    *_, last = reasons(await attempts(sessions, AgentRole.PULSE, trade_case.id))
    assert last[0] == "WAITING"
    # The only reading predates the setup, and by now it is also older than
    # PULSE's observation window: the watch reports no usable price at all.
    assert last[1] in {"OBSERVATION_BEFORE_SETUP", "OBSERVATION_TOO_STALE", "PRICE_UNAVAILABLE"}
    assert "PULSE:TRIGGER_EVIDENCE" not in await produced(sessions, trade_case.id)


async def test_a_setup_that_expired_before_the_next_run_is_not_confirmed(risk_db, now, trace):
    """E: the next pass comes after the setup's ten minutes. Nothing is bought."""
    _, sessions = risk_db
    trade_case, _ = await first_pass(sessions, now, trace)

    summary = await second_pass(sessions, now + SETUP_LIFETIME + timedelta(minutes=1))

    [progress] = summary.cases
    assert progress.status == "READY_FOR_TRIGGER"
    assert summary.fills == 0
    *_, last = reasons(await attempts(sessions, AgentRole.PULSE, trade_case.id))
    assert last == ("WAITING", "SETUP_EXPIRED")
    assert await fills(sessions) == []


async def test_a_normal_v2_candidate_opens_nothing_in_early_only(risk_db, now):
    """F: early-only leaves the normal path alone, in the same replayed run."""
    from tests.scout.test_promotion import NOW, promotable

    _, sessions = risk_db
    await promotable(sessions, 0)

    summary = await run(sessions, replay_settings(), NOW, ports=replay_ports(NOW))

    assert summary.cases_opened == 0
    assert summary.cases == ()
    assert summary.fills == 0


# S4 through the real worker runtime: a wait reason the workflow does not
# permit would fail the task (WAIT_REASON_NOT_PERMITTED), so every ATLAS state
# EARLY waits on must be one its task may wait under.


async def staged_with_onchain(sessions, now, trace, **domains):
    from src.core.clock import FixedClock
    from src.orchestration.workflow.service import TradeCaseService
    from tests.early.conftest import early_onchain
    from tests.riskdata.conftest import record_onchain

    await record_market(sessions, now, price=SPOT)
    cases = TradeCaseService(sessions, clock=FixedClock(now))
    trade_case = await open_early_case(cases, sessions, now, trace)
    payload = early_onchain(now).model_copy(update=domains)
    await record_onchain(cases, trade_case, now, payload, key=f"replay-s4-{trade_case.id}")
    return trade_case


async def test_an_unestablished_atlas_domain_waits_in_the_real_runtime(risk_db, now, trace):
    _, sessions = risk_db
    trade_case = await staged_with_onchain(sessions, now, trace, holder_integrity="UNKNOWN")

    await run(sessions, replay_settings(), now, ports=replay_ports(now))

    assert reasons(await attempts(sessions, AgentRole.EARLY, trade_case.id)) == [
        ("WAITING", "ATLAS_EVIDENCE_UNKNOWN")
    ]


async def test_a_measured_atlas_violation_ends_the_early_task_in_the_real_runtime(
    risk_db, now, trace
):
    _, sessions = risk_db
    trade_case = await staged_with_onchain(sessions, now, trace, contract_integrity="FAIL")

    await run(sessions, replay_settings(), now, ports=replay_ports(now))

    [attempt] = await attempts(sessions, AgentRole.EARLY, trade_case.id)
    assert (attempt.outcome, attempt.reason_code) == ("FAILED_PERMANENT", "ATLAS_EVIDENCE_BLOCKED")
    assert attempt.failure_category == "CAPABILITY_DENIED"


def test_every_atlas_wait_is_one_the_early_task_may_wait_under():
    from src.orchestration.workflow.models import EvidenceAcceptance, EvidenceStatus
    from src.orchestration.workflow.policy import TRADE_CASE_EARLY_V1

    [early] = [task for task in TRADE_CASE_EARLY_V1.tasks if task.role is AgentRole.EARLY]
    assert early.wait is not None
    waits = {"ATLAS_EVIDENCE_PENDING"}
    waits |= {
        f"ATLAS_EVIDENCE_{status.value}"
        for status in EvidenceStatus
        if status not in (EvidenceStatus.AVAILABLE, EvidenceStatus.INVALID)
    }
    waits |= {
        f"ATLAS_EVIDENCE_{acceptance.value}"
        for acceptance in EvidenceAcceptance
        if acceptance not in (EvidenceAcceptance.ACCEPTED, EvidenceAcceptance.BLOCKED)
    }
    assert waits <= early.wait.reasons, waits - early.wait.reasons
