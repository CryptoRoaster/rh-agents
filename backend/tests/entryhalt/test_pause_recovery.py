"""Lifting a durable pause: only the pre-#85 daily loss pause, only into an entry halt.

The pause is produced the way the system produced it — a real BUY through the
real paper executor and the real SENTINEL — so the verdict the origin is read
from is the one the executor wrote. The pre-#85 engine (a daily loss PAUSE)
is reproduced by turning SENTINEL's daily loss REJECT back into PAUSE_SYSTEM.

Everything else is refused and recorded: no pause, an engaged kill switch, a
kill-switch pause, a pause no verdict explains, a record that does not read as
one uninterrupted pause, the wrong verdict named, and no entry halt in force.
Nothing ever lifts a pause on its own.
"""

import os
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from src.core.clock import FixedClock
from src.core.models import RiskLimits, RiskOutcome, TradeIntent, TradingMode
from src.data.tables import AccountRow, PaperControlEventRow
from src.orchestration import paper as paper_module
from src.orchestration.entryhalt.pause import PauseOriginKind
from src.orchestration.entryhalt.service import EntryHaltService
from src.orchestration.paper import PaperTradingService
from src.risk.engine import DAILY_LOSS_LIMIT
from src.runner import control
from tests.entryhalt.conftest import account_of, halt
from tests.riskrequest.conftest import set_account

POSTGRES = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="the audit trigger is PostgreSQL's"
)
LIMIT = RiskLimits().daily_loss_limit_usd


def fresh(intent):
    return TradeIntent(**intent.model_dump(exclude={"id"}))


def operator(sessions, at, **kwargs):
    return EntryHaltService(sessions, clock=FixedClock(at), **kwargs)


async def buy(sessions, at, intent, market, *, limits=None):
    service = PaperTradingService(
        sessions, limits or RiskLimits(), TradingMode.PAPER, clock=FixedClock(at)
    )
    return await service.process(fresh(intent), market)


@pytest.fixture
def legacy(monkeypatch):
    """SENTINEL as it was before #85: a daily loss verdict pauses the system."""
    real = paper_module.evaluate

    def before_85(*args, **kwargs):
        decision = real(*args, **kwargs)
        if DAILY_LOSS_LIMIT in decision.reason_codes:
            return decision.model_copy(update={"outcome": RiskOutcome.PAUSE_SYSTEM})
        return decision

    monkeypatch.setattr(paper_module, "evaluate", before_85)


async def legacy_pause(sessions, now, intent, market):
    await set_account(sessions, realized_loss_today_usd=LIMIT, loss_day=now.date())
    decision = await buy(sessions, now, intent, market)
    assert decision.outcome is RiskOutcome.PAUSE_SYSTEM, decision
    assert (await account_of(sessions)).paused is True
    return decision


async def events(sessions):
    async with sessions() as session:
        return (
            await session.scalars(
                select(PaperControlEventRow)
                .where(PaperControlEventRow.control == "SYSTEM_PAUSE")
                .order_by(PaperControlEventRow.sequence)
            )
        ).all()


# ------------------------------------------------------------------ origin


async def test_a_pause_no_verdict_explains_is_unknown_and_stays(risk_db, now):
    _, sessions = risk_db
    await set_account(sessions, paused=True)

    origin = await operator(sessions, now).pause_origin()
    refused = await operator(sessions, now).clear_pause(
        reason="RECOVERY", actor="kai", confirm_origin=uuid4()
    )

    assert origin.kind is PauseOriginKind.UNKNOWN
    assert refused.refusal.value == "PAUSE_ORIGIN_UNKNOWN"
    assert (await account_of(sessions)).paused is True
    [event] = await events(sessions)
    assert (event.action, event.refusal_code, event.halted_before, event.halted_after) == (
        "REFUSED",
        "PAUSE_ORIGIN_UNKNOWN",
        True,
        True,
    )


async def test_nothing_to_lift_is_refused_and_recorded(risk_db, now):
    _, sessions = risk_db

    refused = await operator(sessions, now).clear_pause(
        reason="RECOVERY", actor=None, confirm_origin=uuid4()
    )

    assert refused.refusal.value == "NOT_PAUSED"
    assert len(await events(sessions)) == 1


async def test_a_kill_switch_pause_is_not_lifted_here(risk_db, now, intent, market):
    _, sessions = risk_db
    decision = await buy(
        sessions, now, intent, market, limits=RiskLimits().model_copy(update={"kill_switch": True})
    )
    assert decision.outcome is RiskOutcome.PAUSE_SYSTEM
    await halt(sessions, "manual", now)

    origin = await operator(sessions, now).pause_origin()
    refused = await operator(sessions, now).clear_pause(
        reason="RECOVERY", actor="kai", confirm_origin=decision.id
    )

    assert (origin.kind, origin.decision_id) == (PauseOriginKind.KILL_SWITCH, decision.id)
    assert refused.refusal.value == "PAUSE_ORIGIN_KILL_SWITCH"
    assert (await account_of(sessions)).paused is True


# --------------------------------------------- the one pause that is lifted


async def test_a_legacy_daily_loss_pause_is_lifted_into_its_halt(
    risk_db, now, intent, market, legacy
):
    _, sessions = risk_db
    decision = await legacy_pause(sessions, now, intent, market)
    # The same verdict set the automatic entry halt beside it (#85).
    assert (await account_of(sessions)).entry_halt_auto is True
    later = now + timedelta(days=2)
    # Days pass; nothing lifts the pause on its own.
    assert (await account_of(sessions)).paused is True

    origin = await operator(sessions, later).pause_origin()
    lifted = await operator(sessions, later).clear_pause(
        reason="LEGACY_DAILY_LOSS_PAUSE_REVIEWED", actor="kai", confirm_origin=decision.id
    )

    assert (origin.kind, origin.decision_id) == (
        PauseOriginKind.LEGACY_DAILY_LOSS_LIMIT,
        decision.id,
    )
    assert lifted.changed, lifted
    account = await account_of(sessions)
    # Sales may resume; purchases stay halted.
    assert (account.paused, account.entry_halt_auto) == (False, True)
    [event] = await events(sessions)
    assert (event.action, event.actor_kind, event.risk_decision_id, event.reason_code) == (
        "CLEAR",
        "OPERATOR",
        decision.id,
        "LEGACY_DAILY_LOSS_PAUSE_REVIEWED",
    )
    assert (event.halted_before, event.halted_after) == (True, False)


async def test_the_wrong_verdict_named_lifts_nothing(risk_db, now, intent, market, legacy):
    _, sessions = risk_db
    await legacy_pause(sessions, now, intent, market)

    refused = await operator(sessions, now).clear_pause(
        reason="RECOVERY", actor="kai", confirm_origin=uuid4()
    )

    assert refused.refusal.value == "PAUSE_ORIGIN_NOT_CONFIRMED"
    assert (await account_of(sessions)).paused is True


async def test_an_engaged_kill_switch_lifts_nothing(risk_db, now, intent, market, legacy):
    _, sessions = risk_db
    decision = await legacy_pause(sessions, now, intent, market)

    refused = await operator(sessions, now, kill_switch=True).clear_pause(
        reason="RECOVERY", actor="kai", confirm_origin=decision.id
    )

    assert refused.refusal.value == "KILL_SWITCH_ENGAGED"
    assert (await account_of(sessions)).paused is True


async def test_without_an_entry_halt_the_pause_stays(
    risk_db, now, intent, market, legacy, monkeypatch
):
    """A pre-#85 pause carried no halt: lifting it would reopen purchases."""
    _, sessions = risk_db

    async def no_halt(*args, **kwargs):
        return False

    monkeypatch.setattr(paper_module, "halt_on_daily_loss", no_halt)
    decision = await legacy_pause(sessions, now, intent, market)

    refused = await operator(sessions, now).clear_pause(
        reason="RECOVERY", actor="kai", confirm_origin=decision.id
    )

    assert refused.refusal.value == "ENTRY_HALT_REQUIRED"
    assert (await account_of(sessions)).paused is True
    # With a manual halt set first, the same request lifts it.
    await halt(sessions, "manual", now)
    lifted = await operator(sessions, now).clear_pause(
        reason="RECOVERY", actor="kai", confirm_origin=decision.id
    )
    assert lifted.changed
    assert (await account_of(sessions)).entry_halt_manual is True


@pytest.mark.skipif(
    bool(os.environ.get("TEST_DATABASE_URL")),
    reason="since 0024 PostgreSQL refuses the unaudited hand edit this reproduces",
)
async def test_a_record_that_is_not_one_uninterrupted_pause_lifts_nothing(
    risk_db, now, intent, market, legacy
):
    """Lifted and set again by hand, with a purchase in between: nothing can be inferred."""
    _, sessions = risk_db
    decision = await legacy_pause(sessions, now, intent, market)
    await set_account(
        sessions,
        paused=False,
        realized_loss_today_usd=0,
        loss_day=now.date(),
        entry_halt_auto=False,
        entry_halt_auto_reason=None,
        entry_halt_auto_at=None,
        entry_halt_auto_loss_day=None,
        entry_halt_auto_decision_id=None,
    )
    approved = await buy(sessions, now, intent, market)
    assert approved.__class__.__name__ == "ExecutionResult", approved
    await set_account(sessions, paused=True)
    await halt(sessions, "manual", now)

    origin = await operator(sessions, now).pause_origin()
    refused = await operator(sessions, now).clear_pause(
        reason="RECOVERY", actor="kai", confirm_origin=decision.id
    )

    assert origin.kind is PauseOriginKind.INCONSISTENT
    assert refused.refusal.value == "PAUSE_HISTORY_INCONSISTENT"
    assert (await account_of(sessions)).paused is True


async def test_a_new_pause_after_a_lift_is_judged_on_its_own_origin(
    risk_db, now, intent, market, legacy
):
    _, sessions = risk_db
    first = await legacy_pause(sessions, now, intent, market)
    assert (
        await operator(sessions, now).clear_pause(
            reason="RECOVERY", actor="kai", confirm_origin=first.id
        )
    ).changed
    later = now + timedelta(days=2)
    # The halt the daily loss left is lifted the regular way, after its day.
    assert (await operator(sessions, later).clear_auto(reason="LOSS_DAY_OVER", actor="kai")).changed
    second = await buy(
        sessions,
        later,
        intent,
        market,
        limits=RiskLimits().model_copy(update={"kill_switch": True}),
    )
    assert (await account_of(sessions)).paused is True

    origin = await operator(sessions, later).pause_origin()

    assert (origin.kind, origin.decision_id) == (PauseOriginKind.KILL_SWITCH, second.id)


# ---------------------------------------------------------------- the CLI


@pytest.mark.parametrize(
    ("argv", "refusal"),
    [
        (["--clear-system-pause", "--reason", "RECOVERY"], "CONFIRM_PAUSE_ORIGIN_REQUIRED"),
        (["--clear-system-pause"], "REASON_REQUIRED"),
        (
            ["--halt-entries", "--reason", "OK", "--confirm-pause-origin", str(uuid4())],
            "CONFIRM_PAUSE_ORIGIN_ONLY_FOR_PAUSE",
        ),
    ],
)
def test_the_cli_refuses_an_incomplete_request_before_any_database(
    monkeypatch, capsys, argv, refusal
):
    import json
    import sys

    def unreachable(*_, **__):  # pragma: no cover - must not be called
        raise AssertionError("connected")

    monkeypatch.setattr(control, "connect", unreachable)
    monkeypatch.setattr(sys, "argv", ["control", *argv])

    assert control.main() == 2
    assert json.loads(capsys.readouterr().out)["refusal"] == refusal


# ------------------------------------------------------------- PostgreSQL


@POSTGRES
async def test_the_database_refuses_a_pause_lifted_without_its_event(risk_db):
    _, sessions = risk_db
    await set_account(sessions, paused=True)

    with pytest.raises(DBAPIError, match="system pause cleared without an audit event"):
        async with sessions.begin() as session:
            await session.execute(update(AccountRow).values(paused=False))

    assert (await account_of(sessions)).paused is True


@POSTGRES
async def test_setting_the_pause_needs_no_event(risk_db):
    _, sessions = risk_db
    async with sessions.begin() as session:
        await session.execute(text("UPDATE paper_accounts SET paused = true"))
    assert (await account_of(sessions)).paused is True


@POSTGRES
async def test_only_an_operator_may_record_a_pause_lift(risk_db, now):
    _, sessions = risk_db
    with pytest.raises(DBAPIError, match="paper_control_event_pause_actor"):
        async with sessions.begin() as session:
            session.add(
                PaperControlEventRow(
                    event_id=uuid4(),
                    occurred_at=now,
                    control="SYSTEM_PAUSE",
                    action="CLEAR",
                    reason_code="RECOVERY",
                    actor_kind="SENTINEL",
                    halted_before=True,
                    halted_after=False,
                )
            )
