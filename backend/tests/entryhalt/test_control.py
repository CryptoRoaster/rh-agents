"""The operator's controls: set, lift, read — always with the audit event.

Lifting the automatic halt is the delicate one: only after its own UTC loss
day, only under SENTINEL's daily limit, and never on its own.
"""

import argparse
import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from src.core.clock import FixedClock
from src.data.tables import PaperControlEventRow
from src.orchestration.entryhalt.service import EntryHaltService
from src.runner import control
from tests.entryhalt.conftest import account_of, event_count, halt
from tests.riskrequest.conftest import set_account


def operator(sessions, at):
    return EntryHaltService(sessions, clock=FixedClock(at))


async def events(sessions):
    async with sessions() as session:
        return (
            await session.scalars(
                select(PaperControlEventRow).order_by(PaperControlEventRow.sequence)
            )
        ).all()


# -------------------------------------------------------------------- manual


async def test_setting_and_lifting_the_manual_halt_each_write_one_event(risk_db, now):
    _, sessions = risk_db

    set_ = await operator(sessions, now).set_manual(reason="OPERATOR_REVIEW", actor="kai")
    account = await account_of(sessions)
    assert set_.changed and set_.status.entry_halt == "MANUAL"
    assert (account.entry_halt_manual, account.entry_halt_manual_reason) == (
        True,
        "OPERATOR_REVIEW",
    )
    cleared = await operator(sessions, now).clear_manual(reason="REVIEW_DONE", actor="kai")

    assert cleared.changed and cleared.status.entry_halt is None
    recorded = await events(sessions)
    assert [
        (item.control, item.action, item.halted_before, item.halted_after) for item in recorded
    ] == [
        ("ENTRY_HALT_MANUAL", "SET", False, True),
        ("ENTRY_HALT_MANUAL", "CLEAR", True, False),
    ]
    assert {item.actor_kind for item in recorded} == {"OPERATOR"}
    assert recorded[0].actor_ref == "kai"


async def test_a_refused_request_changes_nothing_and_is_recorded(risk_db, now):
    _, sessions = risk_db

    refused = await operator(sessions, now).clear_manual(reason="REVIEW_DONE", actor=None)

    assert not refused.changed and refused.refusal.value == "NOT_HALTED"
    [event] = await events(sessions)
    assert (event.action, event.refusal_code) == ("REFUSED", "NOT_HALTED")
    assert (await account_of(sessions)).entry_halt_manual is False


@pytest.mark.parametrize("reason", ["", "lowercase", "WITH SPACE", "1LEADING_DIGIT", "A" * 65])
async def test_a_reason_must_be_a_code(risk_db, now, reason):
    _, sessions = risk_db
    with pytest.raises(ValueError):
        await operator(sessions, now).set_manual(reason=reason, actor=None)
    assert await event_count(sessions) == 0


async def test_an_event_and_its_change_commit_together_or_not_at_all(risk_db, now):
    from src.core.models import RiskLimits
    from src.data.tables import AccountRow
    from src.orchestration.entryhalt.models import ActorKind
    from src.orchestration.entryhalt.service import halt_entries_automatically

    _, sessions = risk_db
    with pytest.raises(RuntimeError):
        async with sessions.begin() as session:
            account = await session.scalar(
                select(AccountRow).where(AccountRow.id == 1).with_for_update()
            )
            await halt_entries_automatically(
                session,
                account,
                reason="DAILY_LOSS_LIMIT",
                at=now,
                actor_kind=ActorKind.SENTINEL,
                limits=RiskLimits(),
            )
            raise RuntimeError("the transaction fails after both were written")

    assert await event_count(sessions) == 0
    assert (await account_of(sessions)).entry_halt_auto is False


async def test_an_operator_cannot_set_the_automatic_halt(risk_db, now):
    from src.core.models import RiskLimits
    from src.data.tables import AccountRow
    from src.orchestration.entryhalt.models import ActorKind
    from src.orchestration.entryhalt.service import halt_entries_automatically

    _, sessions = risk_db
    async with sessions.begin() as session:
        account = await session.scalar(select(AccountRow).where(AccountRow.id == 1))
        with pytest.raises(ValueError):
            await halt_entries_automatically(
                session,
                account,
                reason="DAILY_LOSS_LIMIT",
                at=now,
                actor_kind=ActorKind.OPERATOR,
                limits=RiskLimits(),
            )


# ----------------------------------------------------------------- automatic


async def test_the_automatic_halt_keeps_its_first_reason_and_loss_day(risk_db, now):
    _, sessions = risk_db
    await halt(sessions, "auto", now)
    later = now + timedelta(hours=30)
    from src.core.models import RiskLimits
    from src.data.tables import AccountRow
    from src.orchestration.entryhalt.models import ActorKind
    from src.orchestration.entryhalt.service import halt_entries_automatically

    async with sessions.begin() as session:
        account = await session.scalar(select(AccountRow).where(AccountRow.id == 1))
        assert not await halt_entries_automatically(
            session,
            account,
            reason="ANOTHER_REASON",
            at=later,
            actor_kind=ActorKind.LEDGER,
            limits=RiskLimits(),
        )

    account = await account_of(sessions)
    assert (account.entry_halt_auto_reason, account.entry_halt_auto_loss_day) == (
        "DAILY_LOSS_LIMIT",
        now.date(),
    )
    assert await event_count(sessions) == 1


async def test_the_automatic_halt_is_not_lifted_on_its_own_loss_day(risk_db, now):
    _, sessions = risk_db
    await halt(sessions, "auto", now)

    refused = await operator(sessions, now + timedelta(hours=1)).clear_auto(
        reason="LOSS_DAY_OVER", actor="kai"
    )

    assert refused.refusal.value == "SAME_LOSS_DAY"
    assert (await account_of(sessions)).entry_halt_auto is True
    assert [item.refusal_code for item in await events(sessions)] == [None, "SAME_LOSS_DAY"]


async def test_the_automatic_halt_never_lapses_with_the_day(risk_db, now, trace):
    """A new UTC day resets the day's loss, not the halt: BUYs stay refused."""
    from tests.riskrequest.conftest import build_service, ready_case

    _, sessions = risk_db
    await halt(sessions, "auto", now)
    tomorrow = now + timedelta(days=1)

    status = await operator(sessions, tomorrow).status()
    service = build_service(sessions, tomorrow)
    case = await ready_case(service.cases, tomorrow, trace)
    refused = await service.request_risk_evaluation(case.id, request_key="next-day")

    assert status.entry_halt == "AUTO"
    assert refused.reason.value == "ENTRY_HALTED"


async def test_the_automatic_halt_stays_while_todays_loss_is_at_the_limit(risk_db, now):
    _, sessions = risk_db
    await halt(sessions, "auto", now)
    tomorrow = now + timedelta(days=1)
    # Losses booked again on the new day, up to SENTINEL's daily limit.
    await set_account(sessions, loss_day=tomorrow.date(), realized_loss_today_usd=Decimal("500"))

    refused = await operator(sessions, tomorrow).clear_auto(reason="LOSS_DAY_OVER", actor="kai")

    assert refused.refusal.value == "DAILY_LOSS_LIMIT_REACHED"
    assert (await account_of(sessions)).entry_halt_auto is True


async def test_a_named_loss_day_must_be_the_halts_own(risk_db, now):
    _, sessions = risk_db
    await halt(sessions, "auto", now)
    tomorrow = now + timedelta(days=1)

    wrong = await operator(sessions, tomorrow).clear_auto(
        reason="LOSS_DAY_OVER", actor="kai", confirm_loss_day=date(2000, 1, 1)
    )
    assert wrong.refusal.value == "LOSS_DAY_NOT_CONFIRMED"

    right = await operator(sessions, tomorrow).clear_auto(
        reason="LOSS_DAY_OVER", actor="kai", confirm_loss_day=now.date()
    )

    assert right.changed and right.status.entry_halt is None
    account = await account_of(sessions)
    assert (account.entry_halt_auto, account.entry_halt_auto_loss_day) == (False, None)
    assert [item.action for item in await events(sessions)] == ["SET", "REFUSED", "CLEAR"]


async def test_lifting_one_halt_leaves_the_other(risk_db, now):
    _, sessions = risk_db
    await halt(sessions, "auto", now)
    await halt(sessions, "manual", now)

    await operator(sessions, now).clear_manual(reason="REVIEW_DONE", actor="kai")

    assert (await operator(sessions, now).status()).entry_halt == "AUTO"


async def test_no_halt_change_touches_the_pause(risk_db, now):
    _, sessions = risk_db
    await set_account(sessions, paused=True)
    await halt(sessions, "manual", now)
    await operator(sessions, now).clear_manual(reason="REVIEW_DONE", actor="kai")

    assert (await account_of(sessions)).paused is True


# ----------------------------------------------------------------------- CLI


def arguments(**values):
    defaults = {
        "status": False,
        "events": False,
        "halt_entries": False,
        "clear_manual_halt": False,
        "clear_auto_halt": False,
        "reason": None,
        "actor": None,
        "limit": 20,
        "confirm_loss_day": None,
    }
    return argparse.Namespace(**{**defaults, **values})


async def test_the_cli_sets_reads_and_lists_with_exit_codes(risk_db, now):
    _, sessions = risk_db
    service = operator(sessions, now)

    code, done = await control.perform(
        service, arguments(halt_entries=True, reason="OPERATOR_REVIEW", actor="kai")
    )
    again, refused = await control.perform(
        service, arguments(halt_entries=True, reason="OPERATOR_REVIEW", actor="kai")
    )
    status_code, status = await control.perform(service, arguments(status=True))
    list_code, listed = await control.perform(service, arguments(events=True, limit=5))

    assert (code, done["kind"]) == (0, "entry_halt_changed")
    assert (again, refused["refusal"]) == (2, "ALREADY_HALTED")
    assert (status_code, status["entry_halt"]) == (0, "MANUAL")
    assert list_code == 0 and [item["action"] for item in listed["events"]] == [
        "REFUSED",
        "SET",
    ]
    json.loads(control.render(code, done))


@pytest.mark.parametrize(
    ("argv", "refusal"),
    [
        (["--halt-entries"], "REASON_REQUIRED"),
        (["--clear-auto-halt", "--reason", "lower"], "INVALID_ARGUMENT"),
        (
            ["--halt-entries", "--reason", "OK", "--confirm-loss-day", "2026-01-01"],
            "CONFIRM_LOSS_DAY_ONLY_FOR_AUTO_HALT",
        ),
    ],
)
def test_the_cli_refuses_before_touching_a_database(monkeypatch, capsys, argv, refusal):
    import sys

    def unreachable(*_, **__):  # pragma: no cover - must not be called
        raise AssertionError("connected")

    monkeypatch.setattr(control, "connect", unreachable)
    monkeypatch.setattr(sys, "argv", ["control", *argv])

    assert control.main() == 2
    assert json.loads(capsys.readouterr().out)["refusal"] == refusal


def test_the_cli_offers_exactly_one_action_per_call(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "argv", ["control", "--status", "--events"])
    with pytest.raises(SystemExit) as stopped:
        control.main()
    assert stopped.value.code == 2


def test_no_web_route_writes_a_halt():
    """The API stays read-only: no route of any method but GET exists."""
    from src.api.main import app

    methods = {
        method for route in app.routes for method in getattr(route, "methods", set()) or set()
    }
    assert methods <= {"GET", "HEAD", "OPTIONS"}
