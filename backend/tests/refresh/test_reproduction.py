"""What actually blocks a trigger found after a regular PULSE wait.

No prepared `READY_FOR_RISK` case, no directly submitted specialist evidence and
no skipping the first wait: the case is carried there by the real handlers
through two explicit `--once` passes, with the clock advanced to the next due
check in between and genuinely new observations recorded.

The chain-side sources are deliberately *not* re-observed on the second pass.
That is the gap in its durable form — a trigger found ninety-five seconds after
the case was assembled, judged on a holder reading taken before the wait — and
it stays reproducible after the refresh contract exists, because reading the
same observation again does not make it younger.
"""

from decimal import Decimal

from sqlalchemy import select

from src.core.models import AgentRole
from src.data.tables import TradeCaseRiskBindingRow, TradeCaseRiskRequestRow
from src.orchestration.workflow.models import EvidenceType
from src.runner.service import order_key
from tests.refresh.conftest import (
    ATLAS_SOURCE,
    RECHECK,
    all_specialists,
    attempts,
    chain_sources,
    evidence_rows,
    ports_at,
    record_market_at,
    record_payment_at,
    refreshes,
    scripted,
    task_row,
    traded_case,
)
from tests.runner.conftest import executions, run, stack_for

# Enough of a move to satisfy the setup VECTOR writes, so the trigger really is
# found on the second pass rather than being arranged.
SPOT_UP = Decimal("1.20")


async def carry_to_the_wait(sessions, now, model):
    """One pass that gets as far as a setup and a monitor that is not due."""
    await record_market_at(sessions, now)
    await record_payment_at(sessions, now)
    settings = all_specialists()
    summary = await run(sessions, settings, now, ports=ports_at(now, model))
    return settings, summary


async def test_the_state_after_a_regular_pulse_wait(risk_db, now, trace):
    """The reproduction, recorded fact by fact.

    Run one leaves a setup and a monitor rescheduled by policy. Run two, at the
    next due check and with the market genuinely observed again, finds the
    trigger — and still cannot ask SENTINEL anything, because one source it
    would be judged on was observed before the wait began and has not been
    observed since.
    """
    _, sessions = risk_db
    model = scripted()
    settings, first = await carry_to_the_wait(sessions, now, model)

    assert first.fills == 0
    monitor = await task_row(sessions, AgentRole.PULSE, "WAIT_FOR_TRIGGER")
    assert monitor is not None
    waits = list(await attempts(sessions, AgentRole.PULSE))
    assert [item.outcome for item in waits] == ["WAITING"], waits
    assert monitor.next_eligible_at is not None, "the monitor is rescheduled, not retried"

    # --- the next due check, with the market observed again and the chain not ---
    later = now + RECHECK
    await record_market_at(sessions, later, price=SPOT_UP, label="moved")
    await record_payment_at(sessions, later, label="quote-later")

    second = await run(
        sessions, settings, later, ports=ports_at(later, model, **chain_sources(now))
    )

    # The trigger really is found on this pass, by the real monitor.
    assert any(item.outcome == "SUCCEEDED" for item in await attempts(sessions, AgentRole.PULSE)), (
        second
    )
    trigger = await evidence_rows(sessions, EvidenceType.TRIGGER)
    assert len(trigger) == 1

    case = await traded_case(sessions)
    assert case.status == "READY_FOR_RISK", case.status

    # --- and this is what stops it ---
    progress = next(item for item in second.cases if item.trade_case_id == case.id)
    assert progress.risk_refusal == "SOURCE_OLDER_THAN_RISK_LIMIT", progress
    assert progress.execution_id is None
    assert second.fills == 0
    assert await executions(sessions) == []

    # Which source, and when it was observed: ATLAS's on-chain reading is the one
    # from before the wait. Every other input was observed on this pass.
    onchain = await evidence_rows(sessions, EvidenceType.ONCHAIN)
    assert {item.observed_at.replace(tzinfo=None) for item in onchain} == {
        now.replace(tzinfo=None)
    }, "every on-chain envelope still carries the instant its source was read at"
    assert (later - now).total_seconds() > 30, "older than SENTINEL's own bound"

    # Which check: SENTINEL's own bound, applied to a named source before
    # SENTINEL is asked. Asking the real service again costs nothing and writes
    # nothing — it refuses at the same place — and it names the source.
    again = stack_for(sessions, settings, later, ports=ports_at(later, model))
    verdict = await again.risk.request_risk_evaluation(case.id, request_key=order_key(case.id))
    assert verdict.kind == "risk_request_refused"
    # One source, named: the holder distribution, which only ATLAS observes. The
    # market, token and liquidity readings all come from the snapshot recorded on
    # this pass and pass the same bound.
    assert verdict.detail == "HOLDERS_OLDER_THAN_RISK_LIMIT", verdict

    # The task that produced it was asked to observe again, and did: the real
    # ATLAS handler ran a second time and wrote a second envelope that supersedes
    # the first. What it could not do is make the reading younger, because the
    # chain had not been observed again — so the new envelope carries the same
    # source instant and the refusal is unchanged. A refresh is a new
    # observation or it is nothing.
    assert refreshes(progress) == {ATLAS_SOURCE: "ORDERED"}, progress
    assert len(onchain) == 2
    first_envelope = next(item.evidence_id for item in onchain if item.supersedes_id is None)
    assert [item.supersedes_id for item in onchain if item.supersedes_id is not None] == [
        first_envelope
    ]
    produced_by = await task_row(sessions, AgentRole.ATLAS, "ASSESS_ONCHAIN_INTEGRITY")
    assert [
        (item.attempt_number, item.outcome, item.reason_code)
        for item in await attempts(sessions, AgentRole.ATLAS)
    ] == [(1, "SUCCEEDED", "RESULT_ACCEPTED"), (2, "SUCCEEDED", "RESULT_ACCEPTED")]
    assert produced_by.status == "SUCCEEDED"
    assert produced_by.attempt == 2

    # Nothing final was written: no canonical request, no binding. The case's one
    # request is still unspent.
    async with sessions() as session:
        requests = (await session.scalars(select(TradeCaseRiskRequestRow))).all()
        bindings = (await session.scalars(select(TradeCaseRiskBindingRow))).all()
    assert requests == []
    assert bindings == []
