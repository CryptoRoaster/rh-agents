"""What a bounded discovery read actually brought back, as against what it reserved.

The gap these reproduce
-----------------------

`budget_spent` counts capacity a discovery read was *permitted* to return, and
it is committed before the request leaves. `recorded` counts observations this
run durably wrote. Between the two sits everything the adapter did with the
answer, and none of it was reported: a read that reserved two pools and recorded
one looked exactly the same whether the provider sent one pool, or sent two and
the adapter refused the second for a typed reason.

So `budget_spent=2, recorded=1` was not a statement about the provider at all,
and an operator reading it could not tell a quiet market from a contract breach.

Every test here is the production composition. The one substituted boundary is
the bytes of an HTTP response, in test code, through `httpx.MockTransport`.
A fixture is never evidence that the real provider works.
"""

import httpx
from sqlalchemy import func, select

from src.data.tables import MarketObservationRow
from src.markets.geckoterminal.errors import (
    AuthenticationError,
    BudgetError,
    ClientError,
    ConfigurationError,
    ConnectivityError,
    ContractError,
    IdentityError,
    ProviderError,
    RateLimitError,
    UnavailableError,
    UnsupportedNetworkError,
)
from src.runner.models import AcquisitionStop, ExitCode
from tests.runner.conftest import run
from tests.runner.provider import NETWORK_ID, MarketProvider, pool, traded
from tests.runner.specialists import ScriptedSpecialists
from tests.runner.test_acquisition import acquiring_ports, acquiring_settings
from tests.runner.test_end_to_end import SPOT

# Pools that have nothing to do with the traded market, used only to be refused.
OTHER_POOL = "0x" + "33" * 20
SECOND_POOL = "0x" + "44" * 20
TOKEN = "0x" + "a1" * 20
QUOTE = "0x" + "b2" * 20

# Every fixed code the provider boundary can produce. Stated as the closed set
# it is, so a test can assert that nothing outside it reaches a summary.
PROVIDER_CODES = frozenset(
    item.code.upper()
    for item in (
        ProviderError,
        ConfigurationError,
        UnavailableError,
        ConnectivityError,
        RateLimitError,
        ClientError,
        AuthenticationError,
        ContractError,
        IdentityError,
        UnsupportedNetworkError,
        BudgetError,
    )
)


def misbound(address: str) -> dict:
    """A well-formed pool whose resource id names a different pool.

    Refused inside the adapter's own normalization, which is the point: this is
    a pool the provider delivered and the adapter declined, not a transport
    failure and not a recorder refusal.
    """
    return {
        **pool(address, base=TOKEN, quote=QUOTE, price="1"),
        "id": f"{NETWORK_ID}_{SECOND_POOL}",
    }


def malformed(address: str) -> dict:
    """A resource the pool schema does not accept at all."""
    return {**pool(address, base=TOKEN, quote=QUOTE, price="1"), "type": "pool_like"}


def diagnostics_settings(**overrides):
    """One chain, one discovery read, and a stated per-request pool bound."""
    defaults: dict[str, object] = {
        "paper_runner_acquisition_max_markets": 2,
        "paper_runner_acquisition_max_discovery_requests": 1,
        "geckoterminal_pools_per_chain": 2,
    }
    return acquiring_settings(**{**defaults, **overrides})


def reads(summary):
    return list(summary.acquisition.discovery)


def rejections(read) -> dict[str, int]:
    return {item.reason: item.count for item in read.rejections}


async def recorded_rows(sessions) -> int:
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(MarketObservationRow))


# ------------------------------------- the two answers that used to read alike


async def test_fewer_pools_delivered_is_not_reported_as_a_rejection(risk_db, now, trace):
    """One pool offered against two reserved. Nothing was refused by anybody."""
    _, sessions = risk_db
    provider = MarketProvider(discovery=[traded(SPOT)])

    summary = await run(
        sessions,
        diagnostics_settings(),
        now,
        ports=acquiring_ports(now, ScriptedSpecialists(), provider),
    )

    assert summary.acquisition.budget_spent == 2, summary.acquisition
    assert summary.acquisition.recorded == 1
    (read,) = reads(summary)
    assert read.chain == "robinhood"
    assert read.completed is True
    assert read.reserved == 2
    assert read.considered == 1
    assert read.rejected == 0
    assert read.returned == 1
    assert rejections(read) == {}
    assert read.reason is None
    # Stated here so the rejected-pool case can be compared against it by
    # number rather than by running a second pass over the same database.
    assert summary.acquisition.requested == 0
    assert summary.acquisition.provider_requests == 2
    assert summary.acquisition.http_attempts == 2
    assert len(provider.discovery_requests) == 1, provider.paths
    assert await recorded_rows(sessions) == 1
    assert summary.exit_code is ExitCode.COMPLETED, summary


async def test_a_rejected_pool_is_reported_beside_the_one_that_was_recorded(risk_db, now, trace):
    """Two pools delivered, one declined by the adapter, one recorded.

    The counters above this — `budget_spent=2`, `recorded=1` — are identical to
    the test before it. That identity is the defect these reproduce, and the
    read's own diagnostics are what tells the two situations apart.
    """
    _, sessions = risk_db
    provider = MarketProvider(discovery=[traded(SPOT), misbound(OTHER_POOL)])

    summary = await run(
        sessions,
        diagnostics_settings(),
        now,
        ports=acquiring_ports(now, ScriptedSpecialists(), provider),
    )

    assert summary.acquisition.budget_spent == 2, summary.acquisition
    assert summary.acquisition.recorded == 1
    (read,) = reads(summary)
    assert read.completed is True
    assert read.reserved == 2
    assert read.considered == 2
    assert read.rejected == 1
    assert read.returned == 1
    assert rejections(read) == {"PROVIDER_IDENTITY": 1}
    # The refusal happened inside the adapter, so it is not also a recorder
    # refusal, a transport failure or a market this run asked about by identity.
    assert summary.acquisition.refused == 0, summary.acquisition
    assert summary.acquisition.failed == 0
    assert summary.acquisition.requested == 0
    assert await recorded_rows(sessions) == 1


async def test_an_answer_the_adapter_refuses_entirely_records_nothing(risk_db, now, trace):
    """Every delivered pool declined, each under its own typed code."""
    _, sessions = risk_db
    provider = MarketProvider(discovery=[misbound(OTHER_POOL), malformed(SECOND_POOL)])

    summary = await run(
        sessions,
        diagnostics_settings(),
        now,
        ports=acquiring_ports(now, ScriptedSpecialists(), provider),
    )

    (read,) = reads(summary)
    assert read.completed is True
    assert read.considered == 2
    assert read.rejected == 2
    assert read.returned == 0
    assert rejections(read) == {"PROVIDER_IDENTITY": 1, "PROVIDER_CONTRACT": 1}
    assert summary.acquisition.recorded == 0, summary.acquisition
    assert summary.acquisition.stop == AcquisitionStop.COMPLETED.value
    assert await recorded_rows(sessions) == 0
    # Nothing was refused *by this stage*: the answer never became a market it
    # could ask about or record.
    assert summary.acquisition.refused == 0
    assert summary.acquisition.failed == 0


# ----------------------------------------------- several reads, counted apart


async def test_two_reads_are_reported_once_each_and_never_pooled(risk_db, now, trace):
    """One entry per read, bound to its own chain. No adapter counted twice.

    The second chain resolves, is asked, and refuses everything it is given:
    the pool resources in the answer are bound to the first chain's network, so
    the adapter declines them by identity. Two reads, two accounts.
    """
    _, sessions = risk_db
    provider = MarketProvider(discovery=[traded(SPOT)])

    summary = await run(
        sessions,
        diagnostics_settings(
            market_chains="robinhood,bsc",
            paper_runner_acquisition_max_discovery_requests=2,
            paper_runner_acquisition_max_markets=4,
        ),
        now,
        ports=acquiring_ports(now, ScriptedSpecialists(), provider),
    )

    assert len(provider.discovery_requests) == 2, provider.paths
    first, second = reads(summary)
    assert (first.chain, second.chain) == ("robinhood", "bsc")
    assert (first.reserved, second.reserved) == (2, 2)
    assert (first.considered, first.rejected, first.returned) == (1, 0, 1)
    assert (second.considered, second.rejected, second.returned) == (1, 1, 0)
    assert rejections(first) == {}
    assert rejections(second) == {"PROVIDER_IDENTITY": 1}
    # One recording, from the one read that produced a market.
    assert summary.acquisition.recorded == 1, summary.acquisition
    assert summary.acquisition.budget_spent == 4
    assert await recorded_rows(sessions) == 1


async def test_a_failed_second_read_keeps_the_first_read_s_diagnostics(risk_db, now, trace):
    """Confirmed diagnostics survive a later failure, and claim nothing new.

    The read that failed reports what is actually known about it — the chain it
    was for and the capacity it reserved — and none of the adapter's counters,
    because a read that did not return did not finish counting.
    """
    _, sessions = risk_db

    class FailsTheSecondRead(MarketProvider):
        def handle(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("new_pools") and len(self.discovery_requests) >= 1:
                self.paths.append(request.url.path)
                return httpx.Response(503, text='{"errors": [{"status": "boom"}]}')
            return super().handle(request)

    provider = FailsTheSecondRead(discovery=[traded(SPOT)])

    summary = await run(
        sessions,
        diagnostics_settings(
            market_chains="robinhood,bsc",
            paper_runner_acquisition_max_discovery_requests=2,
            paper_runner_acquisition_max_markets=4,
        ),
        now,
        ports=acquiring_ports(now, ScriptedSpecialists(), provider),
    )

    first, second = reads(summary)
    assert first.completed is True
    assert (first.considered, first.rejected, first.returned) == (1, 0, 1)
    assert second.chain == "bsc"
    assert second.completed is False
    assert second.reserved == 2
    assert (second.considered, second.rejected, second.returned) == (None, None, None)
    assert second.rejections == ()
    assert second.reason == "PROVIDER_UNAVAILABLE"
    assert summary.acquisition.stop == AcquisitionStop.PROVIDER_FAILED.value
    # What was written before the failure stays written.
    assert summary.acquisition.recorded == 1, summary.acquisition
    assert await recorded_rows(sessions) == 1


# ----------------------------------------------- the spend, and what is printed


async def test_a_rejected_pool_costs_exactly_what_a_quiet_answer_costs(risk_db, now, trace):
    """The spend is stated absolutely, and matches the quiet answer's exactly.

    Deliberately one pass and fixed numbers rather than two passes compared to
    each other: a second pass in the same database inherits the first one's
    case and acquires that case's market too, which would be a different run
    rather than the same one with a different answer. The figures below are the
    ones `test_fewer_pools_delivered_is_not_reported_as_a_rejection` asserts for
    the answer that carried no rejected pool.
    """
    _, sessions = risk_db
    provider = MarketProvider(discovery=[traded(SPOT), misbound(OTHER_POOL)])

    summary = await run(
        sessions,
        diagnostics_settings(),
        now,
        ports=acquiring_ports(now, ScriptedSpecialists(), provider),
    )

    assert summary.acquisition.budget_spent == 2, summary.acquisition
    assert summary.acquisition.requested == 0
    assert summary.acquisition.not_attempted == 0
    assert summary.acquisition.provider_requests == 2
    assert summary.acquisition.http_attempts == 2
    assert summary.acquisition.recorded == 1
    # One network resolution and one discovery read. The refused pool did not
    # buy a second request, a retry or a detail lookup.
    assert len(provider.discovery_requests) == 1, provider.paths
    assert provider.multi_requests == [], provider.paths
    assert await recorded_rows(sessions) == 1


async def test_the_reported_diagnostics_carry_only_codes_and_counts(risk_db, now, trace):
    """Nothing from a response reaches the summary: fixed codes and integers.

    The provider's answer carries addresses, symbols and a URL path. What the
    read reports is a chain name this system configured, four integers and
    codes from the boundary's own closed set.
    """
    _, sessions = risk_db
    provider = MarketProvider(
        discovery=[traded(SPOT), misbound(OTHER_POOL), malformed(SECOND_POOL)]
    )

    summary = await run(
        sessions,
        diagnostics_settings(
            paper_runner_acquisition_max_markets=3, geckoterminal_pools_per_chain=3
        ),
        now,
        ports=acquiring_ports(now, ScriptedSpecialists(), provider),
    )

    (read,) = reads(summary)
    assert read.chain in ("robinhood", "bsc")
    assert {
        type(item) for item in (read.reserved, read.considered, read.rejected, read.returned)
    } == {int}
    for item in read.rejections:
        assert item.reason in PROVIDER_CODES, item.reason
        assert isinstance(item.count, int) and item.count >= 1
    rendered = summary.acquisition.model_dump_json()
    for secret in (OTHER_POOL, SECOND_POOL, "new_pools", "api.geckoterminal.com", "pool_like"):
        assert secret not in rendered, secret
