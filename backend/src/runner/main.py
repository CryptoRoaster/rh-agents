"""One bounded PAPER run, or one look at whether it could happen.

    python -m src.runner.main --once        perform exactly one bounded pass
    python -m src.runner.main --preflight   check this configuration, change nothing

Exactly one of the two is required and they cannot be combined. They make
opposite promises — one is allowed to trade and one writes nothing at all — and
a single invocation that did both would leave an operator unable to say which
of them produced what they are reading. There is no daemon flag, no interval and
no scheduler: a run happens because somebody asked for one, and ends when it has
nothing left it may do.

Nothing about this is reachable from the web process. The API never reads
`PAPER_RUNNER_ENABLED` and starts no task, so booting it can never begin a run.

A preflight is **not** an authorization. It says what could be established
locally a moment ago; the executing mode still runs its own refusals, and a
stop committed one second later still stops the run.

Exit codes
----------
Both modes publish the same three-value contract.

``0``  `--once`: the run completed. Cases may be waiting, refused or filled; the
       summary says which, and reaching a budget still counts as completed.
       `--preflight`: everything this process could check locally is in place.
``1``  A technical failure: the database was unreachable, a service raised, or a
       check could not be carried out. Nothing here is a statement about a
       market, and it is the one code that means somebody should look at the
       machine.
``2``  The configuration does not permit a run, or is missing something a run
       needs. Detected before any mutating step, so nothing was attempted and
       nothing was written.

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
from src.runner.composition import RunnerPorts, RunnerStack, runner_stack
from src.runner.models import (
    ConfigurationRefused,
    ExitCode,
    RunReading,
    TechnicalFailure,
)
from src.runner.preflight import PreflightReading, preflight
from src.runner.preflight import refused as preflight_refused
from src.runner.service import BoundedPaperRun


async def run_once(settings: Settings, *, ports: RunnerPorts | None = None) -> RunReading:
    """Execute one pass and dispose of the engine it opened.

    The engine is owned here rather than by the run, so a run that raises still
    releases its connections. Nothing survives this call: no task, no client and
    no background work.
    """
    engine, sessions = connect(settings.database_url)
    try:
        async with runner_stack(settings, sessions, ports=ports) as stack:
            refused = _misconfigured(stack)
            if refused is not None:
                return refused
            return await BoundedPaperRun(stack).execute()
    finally:
        await engine.dispose()


def _misconfigured(stack: RunnerStack) -> ConfigurationRefused | None:
    """An enabled role this configuration cannot run is a mistake, not a result.

    Refused before any mutating step, because proceeding would open cases whose
    evidence nobody could ever produce — and report them as waiting, which reads
    like patience rather than a missing setting.
    """
    broken = stack.misconfigured
    if not broken:
        return None
    return ConfigurationRefused(reason="ROLE_NOT_CONFIGURED", detail=broken[0].reason)


async def check_only(settings: Settings, *, ports: RunnerPorts | None = None) -> PreflightReading:
    """Ask what this configuration could do, and perform none of it.

    Kept beside `run_once` rather than inside it: the two are separate modes,
    and a run that quietly preflighted first would be reporting two different
    things under one contract.
    """
    return await preflight(settings, ports=ports)


def render(reading: RunReading | PreflightReading) -> str:
    """The structured account, as one JSON object of codes and counts.

    Safe by construction: every value is an identifier this system already
    publishes, a count, or a typed reason code. No secret, provider payload or
    exception text can reach here, because none of them is ever put in the model.
    """
    return json.dumps(reading.model_dump(mode="json"), sort_keys=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="One bounded PAPER run, or one preflight.")
    # Mutually exclusive and required: exactly one mode, never both and never
    # neither. Enforced by the parser rather than by a check inside, so an
    # invocation that asked for both is rejected before anything is configured.
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--once",
        action="store_true",
        help="Perform exactly one bounded pass. The only executing mode.",
    )
    mode.add_argument(
        "--preflight",
        action="store_true",
        help="Report what this configuration could do. Writes nothing and calls nobody.",
    )
    arguments = parser.parse_args()
    try:
        settings = Settings()
    except ValidationError:
        # Before anything could write, and without echoing what was misconfigured.
        if arguments.preflight:
            invalid = preflight_refused("SETTINGS_INVALID")
            print(render(invalid))
            return int(invalid.exit_code)
        refused = ConfigurationRefused(reason="SETTINGS_INVALID")
        print(render(refused))
        return int(refused.exit_code)
    if arguments.preflight:
        return _preflight(settings)
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


def _preflight(settings: Settings) -> int:
    """One check, reported whatever happens to it.

    A check that cannot be carried out is a technical failure rather than a
    verdict, and it says so with the same exit code a broken run uses: an
    operator must never read "could not tell" as "not ready".
    """
    try:
        reading = asyncio.run(check_only(settings))
    except KeyboardInterrupt:
        interrupted = preflight_refused("PREFLIGHT_INTERRUPTED")
        print(render(interrupted))
        return int(ExitCode.TECHNICAL_FAILURE)
    except (SQLAlchemyError, OSError, ValueError):
        failed = preflight_refused("PREFLIGHT_STARTUP_FAILED")
        print(render(failed))
        return int(ExitCode.TECHNICAL_FAILURE)
    print(render(reading))
    return int(reading.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
