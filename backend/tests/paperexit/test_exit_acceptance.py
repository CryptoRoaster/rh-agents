"""Offline acceptance of the exit path with several holdings in one run.

Three booked PAPER holdings past their stop in one exit sweep: one whose fresh
ATLAS read cannot establish the holder domain (UNKNOWN), one whose read times
out at the provider, and one healthy. The healthy one is sold; the other two
are refused by name -- typed outcomes, the run not ready -- and nothing raises.
A later run (a restart) sells both, and no holding is ever sold twice. Real
exit service, ledger and ATLAS read; nothing calls a provider. (The holdings
are booked through the shared fixtures, whose entries stand on fixture LP
evidence; the entry side's own acceptance is tests/acceptance/.)
"""

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

from src.core.clock import FixedClock
from src.core.models import RiskLimits
from src.orchestration.exitpolicy.service import AutoExitService
from src.orchestration.paperexit.exitread import ExitReadUnavailable
from tests.paperexit.conftest import build_exit_service, entered, read_account
from tests.paperexit.test_exit_rpc_failures import (
    LATER,
    POLICY,
    SECOND,
    ScriptedClient,
    counts,
    exit_read,
    observed,
)
from tests.riskdata.conftest import IDENTITY, market_for

THIRD = market_for(token="e9" * 20, pool="fa" * 20)
MARKETS = (IDENTITY, SECOND, THIRD)


class Degraded:
    """The real exit read: SECOND's holder domain unknown, THIRD's provider timing out."""

    def __init__(self, inner) -> None:
        self.inner = inner

    async def read(self, trade_case_id, market, request_key):
        if market.base_asset_id == THIRD.base_asset_id:
            raise ExitReadUnavailable("TIMEOUT")
        found = await self.inner.read(trade_case_id, market, request_key)
        if market.base_asset_id == SECOND.base_asset_id:
            payload = found.payload.model_copy(update={"holder_integrity": "UNKNOWN"})
            return replace(found, payload=payload)
        return found


def healthy(at):
    return exit_read(at, ScriptedClient(at, failing="none", code=None, times=0))  # type: ignore[arg-type]


def sweeper(sessions, at, read) -> AutoExitService:
    feed = observed(at, *MARKETS)
    return AutoExitService(
        sessions=sessions,
        exits=build_exit_service(sessions, at, feed=feed, exit_read=read),
        markets=feed,
        policy=POLICY,
        limits=RiskLimits(),
        clock=FixedClock(at),
    )


async def test_one_unknown_one_timeout_one_healthy_in_one_exit_run(risk_db, now) -> None:
    _, sessions = risk_db
    feed = observed(now, *MARKETS, price="1.25")
    await entered(sessions, now, uuid4(), key="first", feed=feed)
    await entered(sessions, now, uuid4(), key="second", feed=feed, identity=SECOND)
    await entered(sessions, now, uuid4(), key="third", feed=feed, identity=THIRD)
    at = now + LATER

    result = await sweeper(sessions, at, Degraded(healthy(at))).sweep()

    assert (result.triggered, result.attempts, result.executed) == (3, 3, 1), result
    assert result.refusals == {"EXIT_RISK_REFUSED": 1, "EXIT_READ_UNAVAILABLE": 1}
    assert (result.blocked, result.failed) == (2, 0)
    assert (await counts(sessions))[0] == 1
    assert (await read_account(sessions)).paused is False

    # A restart: the next run reads the chain again and sells the other two once.
    later = at + timedelta(minutes=1)
    again = await sweeper(sessions, later, healthy(later)).sweep()
    assert (again.triggered, again.executed) == (2, 2), again
    assert (await counts(sessions))[0] == 3

    # And once more: nothing left, nothing sold twice.
    last = at + timedelta(minutes=2)
    done = await sweeper(sessions, last, healthy(last)).sweep()
    assert (done.triggered, done.executed) == (0, 0)
    assert (await counts(sessions))[0] == 3
