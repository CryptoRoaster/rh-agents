"""A chain RPC that fails during an exit's fresh read refuses that sale, by name.

The exit read is ATLAS's real collector over the real `RpcTokenContractSource`;
only the JSON-RPC client underneath is scripted, so no network is touched. A
failing RPC must end in a typed `EXIT_READ_UNAVAILABLE` for that one position:
nothing sold, nothing written, the account untouched — and the sweep goes on
to the next holding, and the same sale succeeds once the chain answers again.
"""

import asyncio
import os
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from src.agents.atlas.context import AtlasSnapshotBuilder
from src.agents.atlas.rpc_source import RpcTokenContractSource
from src.core.clock import FixedClock
from src.core.models import RiskLimits
from src.data.tables import ExecutionRow, TradeCaseExitRow
from src.orchestration.exitpolicy.policy import PaperExitPolicy
from src.orchestration.exitpolicy.service import AutoExitService
from src.orchestration.paperexit.exitread import AtlasExitRead
from src.orchestration.paperexit.models import ExitRefusal
from src.runtime.models import ErrorCode, RuntimeFailure
from tests.atlas.conftest import StubOrigins, holder_source_result, origin_facts
from tests.atlas.conftest import contract_facts as fixture_contract_facts
from tests.atlas.test_rpc_source import FakeClient, config
from tests.casefill.conftest import MultiMarkets
from tests.paperexit.conftest import build_exit_service, entered, read_account, recorded_snapshot
from tests.riskdata.conftest import IDENTITY, market_for

LATER = timedelta(minutes=10)
FRESH = timedelta(seconds=5)
SECOND = market_for(token="c7" * 20, pool="d8" * 20)
POLICY = PaperExitPolicy(stop_loss_bps=2000, take_profit_bps=5000, max_holding_seconds=6 * 3600)


class ScriptedClient(FakeClient):
    """A JSON-RPC client whose chain-head reads fail a given number of times."""

    def __init__(self, at, *, failing: str, code: ErrorCode, times: int = 1_000) -> None:
        super().__init__(
            head=1_000_012,
            timestamp=int(at.timestamp()),
        )
        self.failing, self.code, self.remaining = failing, code, times

    def _maybe_fail(self, method: str) -> None:
        if method == self.failing and self.remaining > 0:
            self.remaining -= 1
            raise RuntimeFailure(self.code)

    async def verify_chain(self) -> int:
        self._maybe_fail("verify_chain")
        return await super().verify_chain()

    async def block_number(self) -> int:
        self._maybe_fail("block_number")
        return await super().block_number()

    async def block(self, number):
        self._maybe_fail("block")
        return await super().block(number)


class AnyTokenHolders:
    """Holder facts for whichever token is asked about, observed at `at`."""

    def __init__(self, at) -> None:
        self.at = at

    async def holder_facts(self, chain, token_address):
        return holder_source_result(self.at, token_address=token_address)


class ChainReads(RpcTokenContractSource):
    """The real RPC source: its chain snapshot — verify, head, pinned block — is
    read through the scripted client exactly as in production. Only the token's
    contract facts come from the ATLAS fixture, which this failure is not about."""

    async def contract_facts(self, token_address, block):
        return fixture_contract_facts()


def exit_read(at, client):
    contracts = ChainReads(client=client, config=config(), clock=FixedClock(at))
    builder = AtlasSnapshotBuilder(
        contracts=contracts,
        holders=AnyTokenHolders(at),
        origins=StubOrigins(origin_facts()),
        clock=FixedClock(at),
    )
    return AtlasExitRead(builder=builder, clock=FixedClock(at))


def observed(at, *markets, price="0.90"):
    snapshots = []
    for identity in markets:
        snapshots.append(
            recorded_snapshot(
                at,
                age=FRESH,
                metadata_age=FRESH,
                pair_id=identity.pair_id,
                base_asset_id=identity.base_asset_id,
                label=f"exit-{identity.pair_id}-{at.isoformat()}",
                price=Decimal(price),
            )
        )
    return MultiMarkets(*snapshots)


async def counts(sessions):
    async with sessions() as session:
        exits = await session.scalar(select(func.count()).select_from(TradeCaseExitRow))
        fills = await session.scalar(select(func.count()).select_from(ExecutionRow))
    return exits, fills


FAILURES = [
    ("verify_chain", ErrorCode.TIMEOUT),
    ("verify_chain", ErrorCode.CONNECTIVITY),
    ("block_number", ErrorCode.UNAVAILABLE),
    ("block", ErrorCode.RATE_LIMITED),
]


@pytest.mark.parametrize(("failing", "code"), FAILURES, ids=[c.value for _, c in FAILURES])
async def test_a_failing_chain_read_refuses_the_sale_by_name(risk_db, now, trace, failing, code):
    _, sessions = risk_db
    _, _, position = await entered(sessions, now, trace)
    cash = (await read_account(sessions)).cash_usd
    at = now + LATER
    client = ScriptedClient(at, failing=failing, code=code)

    result = await build_exit_service(
        sessions, at, feed=observed(at, IDENTITY), exit_read=exit_read(at, client)
    ).execute_position_exit(position.id, request_key="rpc-down")

    assert result.kind == "exit_refused", result
    assert result.reason is ExitRefusal.EXIT_READ_UNAVAILABLE
    assert result.detail == f"ONCHAIN_RPC_{code.value}"
    # Nothing sold, nothing written, nothing paused.
    assert await counts(sessions) == (0, 1)
    account = await read_account(sessions)
    assert account.cash_usd == cash and account.paused is False


async def test_the_same_sale_succeeds_once_the_chain_answers_again(risk_db, now, trace):
    _, sessions = risk_db
    _, _, position = await entered(sessions, now, trace)
    at = now + LATER
    client = ScriptedClient(at, failing="verify_chain", code=ErrorCode.TIMEOUT, times=1)
    service = build_exit_service(
        sessions, at, feed=observed(at, IDENTITY), exit_read=exit_read(at, client)
    )

    first = await service.execute_position_exit(position.id, request_key="retry-1")
    second = await service.execute_position_exit(position.id, request_key="retry-2")
    third = await service.execute_position_exit(position.id, request_key="retry-3")

    assert first.reason is ExitRefusal.EXIT_READ_UNAVAILABLE
    assert second.kind == "paper_exit_recorded", getattr(second, "reason", None)
    assert third.kind == "exit_refused"  # already closed: no second sale
    assert await counts(sessions) == (1, 2)


async def test_a_failing_chain_read_for_one_position_does_not_stop_the_sweep(risk_db, now, trace):
    from uuid import uuid4

    _, sessions = risk_db
    entry_feed = observed(now, IDENTITY, SECOND, price="1.25")
    await entered(sessions, now, trace, key="first", feed=entry_feed)
    await entered(sessions, now, uuid4(), key="second", feed=entry_feed, identity=SECOND)
    at = now + LATER
    # The chain fails exactly once: for whichever holding is swept first.
    client = ScriptedClient(at, failing="verify_chain", code=ErrorCode.CONNECTIVITY, times=1)
    feed = observed(at, IDENTITY, SECOND)
    sweep = AutoExitService(
        sessions=sessions,
        exits=build_exit_service(sessions, at, feed=feed, exit_read=exit_read(at, client)),
        markets=feed,
        policy=POLICY,
        limits=RiskLimits(),
        clock=FixedClock(at),
    )

    result = await sweep.sweep()

    assert (result.evaluated, result.triggered, result.executed) == (2, 2, 1), result
    assert result.refusals == {"EXIT_READ_UNAVAILABLE": 1}
    assert (await counts(sessions))[0] == 1
    assert (await read_account(sessions)).paused is False

    # The refused holding is still open and is sold on the next sweep.
    later = at + timedelta(minutes=1)
    fresh = observed(later, IDENTITY, SECOND)
    again = await AutoExitService(
        sessions=sessions,
        exits=build_exit_service(sessions, later, feed=fresh, exit_read=exit_read(later, client)),
        markets=fresh,
        policy=POLICY,
        limits=RiskLimits(),
        clock=FixedClock(later),
    ).sweep()
    assert again.executed == 1
    assert (await counts(sessions))[0] == 2


async def test_an_early_exit_survives_a_failing_chain_and_retries(risk_db, now):
    from tests.early.test_exit import early_entry, observe, sweeper

    _, sessions = risk_db
    await early_entry(sessions, now)
    at = now + timedelta(minutes=10)
    await observe(sessions, at, price="0.40")
    client = ScriptedClient(at, failing="block_number", code=ErrorCode.TIMEOUT, times=1)

    blocked = await sweeper(sessions, at, read=exit_read(at, client)).sweep()
    assert blocked.triggers == {"STOP_LOSS": 1} and blocked.executed == 0, blocked
    assert blocked.refusals == {"EXIT_READ_UNAVAILABLE": 1}

    later = at + timedelta(minutes=1)
    await observe(sessions, later, price="0.40")
    done = await sweeper(sessions, later, read=exit_read(later, client)).sweep()
    assert done.executed == 1
    assert (await counts(sessions))[0] == 1


async def test_a_programming_error_is_not_turned_into_a_refusal(risk_db, now, trace):
    """Only the RPC's own typed failures are mapped; a defect still surfaces."""
    _, sessions = risk_db
    _, _, position = await entered(sessions, now, trace)
    at = now + LATER

    class Broken(FakeClient):
        async def verify_chain(self) -> int:
            raise AttributeError("a defect, not an outage")

    with pytest.raises(AttributeError):
        await build_exit_service(
            sessions, at, feed=observed(at, IDENTITY), exit_read=exit_read(at, Broken())
        ).execute_position_exit(position.id, request_key="defect")
    assert await counts(sessions) == (0, 1)


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required")
async def test_racing_sales_with_a_flaky_chain_sell_once(risk_db, now, trace):
    _, sessions = risk_db
    _, _, position = await entered(sessions, now, trace)
    at = now + LATER
    client = ScriptedClient(at, failing="verify_chain", code=ErrorCode.TIMEOUT, times=1)
    feed = observed(at, IDENTITY)

    results = await asyncio.gather(
        *(
            build_exit_service(
                sessions, at, feed=feed, exit_read=exit_read(at, client)
            ).execute_position_exit(position.id, request_key=f"race-{index}")
            for index in range(3)
        )
    )

    assert sum(item.kind == "paper_exit_recorded" for item in results) == 1
    assert (await counts(sessions))[0] == 1
    assert (await read_account(sessions)).paused is False


async def test_a_failing_chain_read_does_not_end_the_paper_run(risk_db, now, trace):
    """The whole bounded run completes, reports the refusal, and pauses nothing."""
    from dataclasses import replace

    from src.runner.service import BoundedPaperRun
    from tests.runner.conftest import runner_settings, stack_for

    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + LATER
    client = ScriptedClient(at, failing="verify_chain", code=ErrorCode.TIMEOUT)
    feed = observed(at, IDENTITY)
    stack = stack_for(sessions, runner_settings(), at)
    stack = replace(
        stack,
        exits=AutoExitService(
            sessions=sessions,
            exits=build_exit_service(sessions, at, feed=feed, exit_read=exit_read(at, client)),
            markets=feed,
            policy=POLICY,
            limits=RiskLimits(),
            clock=FixedClock(at),
        ),
    )

    summary = await BoundedPaperRun(stack).execute()

    assert summary.exits is not None
    assert (summary.exits.triggered, summary.exits.executed) == (1, 0)
    assert summary.exits.refusals == ("EXIT_READ_UNAVAILABLE",)
    assert summary.errors == ()
    assert (await counts(sessions))[0] == 0
    assert (await read_account(sessions)).paused is False
