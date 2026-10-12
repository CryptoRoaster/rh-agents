"""The operator's controls for the entry-only halt and the pause. One action per call.

    python -m src.runner.control --status
    python -m src.runner.control --events [--limit N]
    python -m src.runner.control --halt-entries      --reason CODE [--actor REF]
    python -m src.runner.control --clear-manual-halt --reason CODE [--actor REF]
    python -m src.runner.control --clear-auto-halt   --reason CODE [--actor REF]
                                                     [--confirm-loss-day YYYY-MM-DD]
    python -m src.runner.control --pause-origin
    python -m src.runner.control --clear-system-pause --reason CODE [--actor REF]
                                                     --confirm-pause-origin DECISION_ID

A halt stops every new BUY on every path and no SELL. The durable pause and the
kill switch — the full stops — are never set here, and the kill switch is never
lifted here. The pause is lifted only in one case: it was set by the daily loss
limit before that limit halted entries instead (#85), the record shows nothing
else since, the operator names that verdict (`--pause-origin` shows it), the
kill switch is off in this configuration, and an entry halt is already in force
— so sales resume and purchases stay halted. Every other pause is refused and
needs the recovery in `docs/paper-minimum-release-gate.md`.

**Who may use this.** Whoever can run this module against the database: the
same host, shell and `DATABASE_URL` access that applies migrations. Nothing
here authenticates a person. `--actor` is whatever the caller names itself; it
is recorded with the event and never verified, so it identifies nobody on its
own. There is deliberately no web route and no agent tool that writes a halt:
the API stays read-only, and no model is ever handed this.

**What it guarantees.** Every change is one transaction on the locked paper
account and writes its audit event with it; a refused request is recorded too.
The automatic halt is lifted only once its UTC loss day is over and the day's
realised loss is under SENTINEL's daily limit, both read under the lock.

Exit codes: ``0`` done (or read), ``1`` technical failure, ``2`` refused or invalid.
Output is one JSON object of codes, counts and instants — never a secret.
"""

import argparse
import asyncio
import json
from datetime import date
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from src.core.config import Settings, settings_refusal
from src.data.database import connect
from src.orchestration.entryhalt.models import EntryHaltUnavailable, reason_code
from src.orchestration.entryhalt.service import EntryHaltService, actor_reference

COMPLETED, TECHNICAL_FAILURE, REFUSED = 0, 1, 2


async def perform(service: EntryHaltService, arguments: argparse.Namespace) -> tuple[int, Any]:
    """One action against one service. Returns the exit code and what to print."""
    if arguments.status:
        return COMPLETED, (await service.status()).model_dump(mode="json")
    if arguments.events:
        events = await service.events(arguments.limit)
        return COMPLETED, {"events": [item.model_dump(mode="json") for item in events]}
    if arguments.pause_origin:
        return COMPLETED, (await service.pause_origin()).model_dump(mode="json")
    if arguments.clear_system_pause:
        outcome = await service.clear_pause(
            reason=arguments.reason,
            actor=arguments.actor,
            confirm_origin=arguments.confirm_pause_origin,
        )
    elif arguments.halt_entries:
        outcome = await service.set_manual(reason=arguments.reason, actor=arguments.actor)
    elif arguments.clear_manual_halt:
        outcome = await service.clear_manual(reason=arguments.reason, actor=arguments.actor)
    else:
        outcome = await service.clear_auto(
            reason=arguments.reason,
            actor=arguments.actor,
            confirm_loss_day=arguments.confirm_loss_day,
        )
    return (COMPLETED if outcome.changed else REFUSED), outcome.model_dump(mode="json")


async def _run(settings: Settings, arguments: argparse.Namespace) -> tuple[int, Any]:
    engine, sessions = connect(settings.database_url)
    try:
        service = EntryHaltService(sessions, kill_switch=settings.commander_kill_switch)
        return await perform(service, arguments)
    finally:
        await engine.dispose()


def parser() -> argparse.ArgumentParser:
    parse = argparse.ArgumentParser(description="Read, set or lift the entry-only halt.")
    action = parse.add_mutually_exclusive_group(required=True)
    action.add_argument("--status", action="store_true", help="Read every control.")
    action.add_argument("--events", action="store_true", help="List the latest audit events.")
    action.add_argument("--halt-entries", action="store_true", help="Set the manual halt.")
    action.add_argument("--clear-manual-halt", action="store_true", help="Lift the manual halt.")
    action.add_argument(
        "--clear-auto-halt",
        action="store_true",
        help="Lift the automatic halt after its loss day, under the daily loss limit.",
    )
    action.add_argument(
        "--pause-origin", action="store_true", help="Read where the durable pause came from."
    )
    action.add_argument(
        "--clear-system-pause",
        action="store_true",
        help="Lift a pre-#85 daily loss pause into an entry halt. Nothing else.",
    )
    parse.add_argument("--reason", help="Required for every change: an UPPER_SNAKE_CASE code.")
    parse.add_argument("--actor", help="Who is asking, as recorded. Never verified.")
    parse.add_argument("--limit", type=int, default=20, help="Events to list (1-500).")
    parse.add_argument(
        "--confirm-loss-day",
        type=date.fromisoformat,
        help="The automatic halt's own UTC loss day, to lift exactly that halt.",
    )
    parse.add_argument(
        "--confirm-pause-origin",
        type=UUID,
        help="The verdict that set the pause (see --pause-origin), to lift exactly that pause.",
    )
    return parse


def render(code: int, document: Any) -> str:
    return json.dumps({"exit_code": code, **document}, sort_keys=True, default=str)


def main() -> int:
    arguments = parser().parse_args()
    changing = not (arguments.status or arguments.events or arguments.pause_origin)
    if changing and not arguments.reason:
        print(render(REFUSED, {"refusal": "REASON_REQUIRED"}))
        return REFUSED
    if arguments.confirm_loss_day is not None and not arguments.clear_auto_halt:
        print(render(REFUSED, {"refusal": "CONFIRM_LOSS_DAY_ONLY_FOR_AUTO_HALT"}))
        return REFUSED
    if arguments.clear_system_pause and arguments.confirm_pause_origin is None:
        print(render(REFUSED, {"refusal": "CONFIRM_PAUSE_ORIGIN_REQUIRED"}))
        return REFUSED
    if arguments.confirm_pause_origin is not None and not arguments.clear_system_pause:
        print(render(REFUSED, {"refusal": "CONFIRM_PAUSE_ORIGIN_ONLY_FOR_PAUSE"}))
        return REFUSED
    try:
        if changing:
            reason_code(arguments.reason)
            actor_reference(arguments.actor)
        if not 1 <= arguments.limit <= 500:
            raise ValueError("Between 1 and 500 events may be listed")
    except ValueError as error:
        # The message names the rule that was broken, never a stored value.
        print(render(REFUSED, {"refusal": "INVALID_ARGUMENT", "detail": str(error)}))
        return REFUSED
    try:
        settings = Settings()
    except ValidationError as error:
        reason = settings_refusal([str(item.get("msg", "")) for item in error.errors()])
        print(render(REFUSED, {"refusal": reason}))
        return REFUSED
    try:
        code, document = asyncio.run(_run(settings, arguments))
    except EntryHaltUnavailable as error:
        print(render(TECHNICAL_FAILURE, {"failure": error.reason_code}))
        return TECHNICAL_FAILURE
    except (SQLAlchemyError, OSError):
        print(render(TECHNICAL_FAILURE, {"failure": "DATABASE_UNAVAILABLE"}))
        return TECHNICAL_FAILURE
    print(render(code, document))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
