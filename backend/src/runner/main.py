"""One bounded PAPER run: python -m src.runner.main --once

`--once` is required and is the only mode there is. There is no daemon flag, no
interval and no scheduler — a run happens because somebody asked for one, and
ends when it has nothing left it may do.

Nothing about this is reachable from the web process. The API never reads
`PAPER_RUNNER_ENABLED` and starts no task, so booting it can never begin a run.

Exit codes
----------
``0``  The run completed. Cases may be waiting, refused or filled; the summary
       says which, and reaching a budget still counts as completed.
``1``  A technical failure: the database was unreachable, or a service raised.
       Nothing here is a statement about a market.
``2``  The configuration does not permit a run. Detected before any mutating
       step, so nothing was attempted and nothing was written.

A business refusal — SENTINEL declining, evidence missing, a case waiting — is
**not** an error exit. The run did what it exists to do and reported a stop;
treating that as an outage would teach an operator to ignore the one code that
means something is actually broken.
"""

import argparse
import asyncio
import json

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from src.core.config import Settings
from src.data.database import connect
from src.runner.composition import RunnerPorts, build_stack
from src.runner.models import (
    ConfigurationRefused,
    RunReading,
    TechnicalFailure,
)
from src.runner.service import BoundedPaperRun


async def run_once(settings: Settings, *, ports: RunnerPorts | None = None) -> RunReading:
    """Execute one pass and dispose of the engine it opened.

    The engine is owned here rather than by the run, so a run that raises still
    releases its connections. Nothing survives this call: no task, no client and
    no background work.
    """
    engine, sessions = connect(settings.database_url)
    try:
        stack = build_stack(settings, sessions, ports=ports)
        return await BoundedPaperRun(stack).execute()
    finally:
        await engine.dispose()


def render(reading: RunReading) -> str:
    """The structured account, as one JSON object of codes and counts.

    Safe by construction: every value is an identifier this system already
    publishes, a count, or a typed reason code. No secret, provider payload or
    exception text can reach here, because none of them is ever put in the model.
    """
    return json.dumps(reading.model_dump(mode="json"), sort_keys=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="One bounded PAPER run.")
    parser.add_argument(
        "--once",
        action="store_true",
        required=True,
        help="Perform exactly one bounded pass. The only supported mode.",
    )
    parser.parse_args()
    try:
        settings = Settings()
    except ValidationError:
        # Before anything could write, and without echoing what was misconfigured.
        refused = ConfigurationRefused(reason="SETTINGS_INVALID")
        print(render(refused))
        return int(refused.exit_code)
    try:
        reading = asyncio.run(run_once(settings))
    except KeyboardInterrupt:
        # Whatever had committed stays committed; the interrupted step follows
        # its own contract, and a lease left behind expires for recovery.
        failure = TechnicalFailure(reason="RUN_INTERRUPTED")
        print(render(failure))
        return int(failure.exit_code)
    except (SQLAlchemyError, OSError, ValueError):
        failure = TechnicalFailure(reason="RUN_STARTUP_FAILED")
        print(render(failure))
        return int(failure.exit_code)
    print(render(reading))
    return int(reading.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
