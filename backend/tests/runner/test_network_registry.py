"""The run's registry of validated chain → provider network bindings.

The real public `/networks` list is paginated, and Robinhood is on its third
page (see docs/phase-1.md). Every fixture below uses that layout — BSC on page
one, an unrelated network on page two, Robinhood on page three — so the request
counts asserted here are the counts the real layout would produce.

The registry never replaces validation. It only remembers, for the rest of one
PAPER run, a binding the existing `NetworkDirectory.resolve()` path has already
validated; a failure is never remembered, and a second run starts empty.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from src.core.clock import FixedClock
from src.core.models import Position
from src.data.repository import save_position
from src.markets.geckoterminal.adapter import GeckoTerminalAdapter
from src.markets.geckoterminal.errors import (
    BudgetError,
    ConfigurationError,
    ContractError,
    UnsupportedNetworkError,
)
from src.markets.geckoterminal.networks import (
    CHAINS,
    Chain,
    NetworkDirectory,
    VerifiedNetworkRegistry,
)
from src.markets.geckoterminal.transport import GeckoTerminalTransport
from src.markets.recorder import MarketRecorder, record_pair_reporting
from src.runner.pre_risk import PreRiskReason
from src.runner.service import BoundedPaperRun, Deadline
from tests.runner.conftest import stack_for
from tests.runner.provider import NETWORKS, MarketProvider, payment, pool, traded
from tests.runner.specialists import ScriptedSpecialists
from tests.runner.test_acquisition import (
    PAIR_ID,
    acquiring_ports,
    acquiring_settings,
    full_acquiring_settings,
)
from tests.runner.test_end_to_end import SPOT, traded_case
from tests.runner.test_pre_risk import STALE, case_on, identity, stage

BSC_NETWORK, RH_NETWORK = NETWORKS["data"]
FILLER = {
    "id": "eth",
    "type": "network",
    "attributes": {"name": "Ethereum", "coingecko_asset_platform_id": "ethereum"},
}
# The real public layout: BSC first, Robinhood on the third page.
REAL = [[BSC_NETWORK], [FILLER], [RH_NETWORK]]
ONE_PAGE = [[BSC_NETWORK, RH_NETWORK]]

BSC_POOL = "0x" + "b5" * 20
BSC_BASE = "0x" + "b6" * 20
BSC_PAIR_ID = f"bsc:mainnet:contract_address:{BSC_POOL}"


def bsc_pool(price="2"):
    return pool(BSC_POOL, base=BSC_BASE, quote="0x" + "b7" * 20, price=price, network="bsc")


class Paged(MarketProvider):
    """The pool reads of `MarketProvider`, with `/networks` answered page by page."""

    def __init__(self, pages=REAL, **kwargs):
        super().__init__(**kwargs)
        self.pages = pages

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/networks"):
            self.requests.append(request)
            self.paths.append(request.url.path)
            page = int(request.url.params["page"])
            last = page >= len(self.pages)
            return httpx.Response(
                200,
                json={
                    "data": self.pages[page - 1] if page <= len(self.pages) else [],
                    "links": {"next": None if last else "next"},
                },
            )
        return super().handle(request)

    @property
    def network_requests(self) -> int:
        return len([item for item in self.paths if item.endswith("/networks")])


def transport_for(provider, **overrides):
    settings = acquiring_settings(market_chains="robinhood,bsc", **overrides)
    return settings, GeckoTerminalTransport(settings, transport=provider.transport())


async def resolve(provider, chain, registry, **overrides):
    settings, transport = transport_for(provider, **overrides)
    try:
        directory = NetworkDirectory(transport, settings, registry=registry)
        return await directory.resolve(chain), directory
    finally:
        await transport.__aexit__(None, None, None)


# ------------------------------------------------------------------------- cache


async def test_bsc_is_validated_once_then_served_from_the_run_registry():
    registry = VerifiedNetworkRegistry()
    first = Paged()
    network, directory = await resolve(first, CHAINS["bsc"], registry)
    assert (network, first.network_requests) == ("bsc", 1)
    assert directory.resolved_by_provider == 1
    assert registry.verified("bsc") == "bsc"

    second = Paged()
    network, directory = await resolve(second, CHAINS["bsc"], registry)
    assert (network, second.network_requests) == ("bsc", 0)
    assert directory.resolved_from_cache == 1


async def test_robinhood_is_validated_through_its_real_page_then_cached():
    registry = VerifiedNetworkRegistry()
    first = Paged()
    network, _ = await resolve(first, CHAINS["robinhood"], registry)
    assert (network, first.network_requests) == ("robinhood", 3)
    assert registry.verified("robinhood") == "robinhood"

    second = Paged()
    network, _ = await resolve(second, CHAINS["robinhood"], registry)
    assert (network, second.network_requests) == ("robinhood", 0)


async def test_without_a_registry_nothing_is_remembered():
    provider = Paged()
    await resolve(provider, CHAINS["bsc"], None)
    await resolve(provider, CHAINS["bsc"], None)
    assert provider.network_requests == 2


async def test_an_entry_for_another_configured_id_is_not_trusted():
    """The registry answers only for the id this configuration names."""
    registry = VerifiedNetworkRegistry()
    registry.record("robinhood", "somewhere-else")
    provider = Paged()
    network, directory = await resolve(provider, CHAINS["robinhood"], registry)
    assert (network, provider.network_requests) == ("robinhood", 3)
    assert directory.resolved_from_cache == 0
    assert registry.verified("robinhood") == "robinhood"


# ------------------------------------------------------------ failures are not kept


WRONG_PLATFORM = {
    "id": "robinhood",
    "type": "network",
    "attributes": {"name": "Robinhood", "coingecko_asset_platform_id": "ethereum"},
}
CONFLICTING = {
    "id": "bsc",
    "type": "network",
    "attributes": {"name": "Other", "coingecko_asset_platform_id": "binance-smart-chain"},
}


@pytest.mark.parametrize(
    ("pages", "chain", "overrides", "error"),
    [
        # The configured scan stops before the page Robinhood is on.
        (REAL, "robinhood", {"geckoterminal_network_pages": 2}, BudgetError),
        # The transport's own request budget ends first.
        (REAL, "robinhood", {"geckoterminal_max_requests": 2}, BudgetError),
        # A network answered with the wrong platform binding.
        ([[BSC_NETWORK], [FILLER], [WRONG_PLATFORM]], "robinhood", {}, UnsupportedNetworkError),
        # Not published at all.
        ([[BSC_NETWORK], [FILLER]], "robinhood", {}, UnsupportedNetworkError),
        # The same id answered two different ways across pages.
        ([[BSC_NETWORK], [CONFLICTING], [RH_NETWORK]], "robinhood", {}, ContractError),
        # A document that does not match the published contract.
        ([[{"id": "robinhood", "type": "network"}]], "robinhood", {}, ContractError),
    ],
)
async def test_a_failed_validation_is_never_cached(pages, chain, overrides, error):
    registry = VerifiedNetworkRegistry()
    with pytest.raises(error):
        await resolve(Paged(pages), CHAINS[chain], registry, **overrides)
    assert len(registry) == 0


async def test_a_chain_that_is_not_the_registered_one_is_refused_and_not_cached():
    registry = VerifiedNetworkRegistry()
    impostor = Chain("robinhood", 1, "robinhood")
    with pytest.raises(ConfigurationError):
        await resolve(Paged(), impostor, registry)
    assert len(registry) == 0


async def test_a_timeout_during_validation_is_never_cached():
    registry = VerifiedNetworkRegistry()
    provider = Paged()

    async def slow(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "3":
            await asyncio.sleep(5)
        return provider.handle(request)

    settings = acquiring_settings(market_chains="robinhood,bsc")
    transport = GeckoTerminalTransport(settings, transport=httpx.MockTransport(slow))
    try:
        directory = NetworkDirectory(transport, settings, registry=registry)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(directory.resolve(CHAINS["robinhood"]), timeout=0.5)
    finally:
        await transport.__aexit__(None, None, None)
    # BSC was on a page already read, but nothing was validated to completion.
    assert len(registry) == 0


# ----------------------------------------------------------- the pre-risk stage


async def seed_on(sessions, at, provider_pools):
    """Record pools on both chains through the production adapter."""
    provider = Paged(ONE_PAGE, discovery_by_network=provider_pools)
    settings, transport = transport_for(provider)
    clock = FixedClock(at)
    transport = GeckoTerminalTransport(settings, transport=provider.transport(), clock=clock)
    try:
        directory = NetworkDirectory(transport, settings)
        recorder = MarketRecorder(sessions, clock=clock)
        for name in provider_pools:
            adapter = GeckoTerminalAdapter(
                transport, directory, CHAINS[name], settings, clock=clock
            )
            for pair in await adapter.discover():
                await record_pair_reporting(adapter, pair, recorder)
    finally:
        await transport.__aexit__(None, None, None)


async def hold_bsc(sessions, now, trace):
    async with sessions.begin() as session:
        await save_position(
            session,
            Position(
                source="LEDGER",
                correlation_id=trace,
                asset_id=f"bsc:mainnet:{BSC_BASE}",
                market_pair_id=BSC_PAIR_ID,
                market_chain="bsc",
                market_network="mainnet",
                market_provider="geckoterminal",
                quantity=Decimal("1"),
                cost_basis_usd=Decimal("2"),
                created_at=now,
                updated_at=now,
            ),
        )


async def warm(chains):
    """A registry filled the only permitted way: by validating each chain."""
    registry = VerifiedNetworkRegistry()
    for name in chains:
        await resolve(Paged(), CHAINS[name], registry)
    return registry


async def test_a_warm_registry_leaves_robinhood_one_pool_batch(risk_db, now, trace):
    _, sessions = risk_db
    await seed_on(sessions, now - STALE, {"robinhood": [traded(SPOT)]})
    provider = Paged(targeted=[traded(SPOT)])

    reading = await stage(sessions, now, provider, market_chains="robinhood,bsc").refresh(
        case_on(await identity(sessions, now)), Deadline(60), networks=await warm(["robinhood"])
    )

    assert reading.ready, reading
    assert reading.provider_requests == 1
    assert provider.network_requests == 0
    assert (reading.network_resolution_cache, reading.network_resolution_provider) == (1, 0)


async def test_a_warm_registry_leaves_robinhood_and_bsc_two_batches(risk_db, now, trace):
    _, sessions = risk_db
    await seed_on(sessions, now - STALE, {"robinhood": [traded(SPOT)], "bsc": [bsc_pool()]})
    await hold_bsc(sessions, now, trace)
    provider = Paged(targeted=[traded(SPOT), bsc_pool()])

    reading = await stage(sessions, now, provider, market_chains="robinhood,bsc").refresh(
        case_on(await identity(sessions, now)),
        Deadline(60),
        networks=await warm(["robinhood", "bsc"]),
    )

    assert reading.ready, reading
    assert reading.markets == (PAIR_ID, BSC_PAIR_ID)
    assert reading.provider_requests == 2, "one exact batch per chain, nothing else"
    assert provider.network_requests == 0
    assert sorted(
        item.split("/networks/")[1].split("/")[0] for item in provider.multi_requests
    ) == [
        "bsc",
        "robinhood",
    ]


async def test_a_cold_registry_validates_normally_and_the_budget_still_binds(risk_db, now, trace):
    """Robinhood's three pages spend the whole default budget; no request follows."""
    _, sessions = risk_db
    await seed_on(sessions, now - STALE, {"robinhood": [traded(SPOT)]})
    provider = Paged(targeted=[traded(SPOT)])
    registry = VerifiedNetworkRegistry()

    reading = await stage(sessions, now, provider, market_chains="robinhood,bsc").refresh(
        case_on(await identity(sessions, now)), Deadline(60), networks=registry
    )

    assert not reading.ready
    assert reading.reason == PreRiskReason.REQUEST_BUDGET_REACHED.value
    assert provider.network_requests == 3 and provider.multi_requests == []
    # What was validated in full is remembered, and nothing else.
    assert registry.verified("robinhood") == "robinhood"
    assert reading.network_resolution_provider == 1


async def test_a_cold_registry_on_a_short_network_list_still_refreshes(risk_db, now, trace):
    _, sessions = risk_db
    await seed_on(sessions, now - STALE, {"robinhood": [traded(SPOT)]})
    provider = Paged(ONE_PAGE, targeted=[traded(SPOT)])
    registry = VerifiedNetworkRegistry()

    reading = await stage(sessions, now, provider, market_chains="robinhood,bsc").refresh(
        case_on(await identity(sessions, now)), Deadline(60), networks=registry
    )

    assert reading.ready, reading
    assert reading.provider_requests == 2
    assert registry.verified("robinhood") == "robinhood"


# ----------------------------------------------------------------------- the run


def robinhood_run(**overrides):
    return full_acquiring_settings(paper_runner_acquisition_max_discovery_requests=0, **overrides)


async def opened(sessions, now, model):
    await BoundedPaperRun(
        stack_for(
            sessions,
            full_acquiring_settings(pulse_worker_enabled=False, anchor_worker_enabled=False),
            now,
            ports=acquiring_ports(now, model, Paged(discovery=[traded(SPOT), payment()])),
        )
    ).execute()
    case = await traded_case(sessions)
    assert case is not None
    return case


async def test_the_run_start_acquisition_validates_and_pre_risk_reuses_it(risk_db, now, trace):
    """Robinhood on page three, no discovery: acquisition scans, pre-risk does not."""
    _, sessions = risk_db
    model = ScriptedSpecialists()
    case = await opened(sessions, now, model)
    later = now + timedelta(seconds=20)
    provider = Paged(targeted=[traded(SPOT * Decimal("1.20")), payment()])

    summary = await BoundedPaperRun(
        stack_for(sessions, robinhood_run(), later, ports=acquiring_ports(later, model, provider))
    ).execute()

    progress = next(item for item in summary.cases if item.trade_case_id == case)
    (refreshed,) = progress.market_refreshes
    assert refreshed.ready, refreshed
    assert refreshed.provider_requests == 1, "only the exact pool batch"
    assert (refreshed.network_resolution_cache, refreshed.network_resolution_provider) == (1, 0)
    # All three network pages were read once in this run, by the acquisition.
    assert provider.network_requests == 3
    assert progress.risk_outcome == "APPROVE", progress


async def test_a_second_pre_risk_refresh_after_a_source_refresh_is_pool_reads_only(
    risk_db, now, trace
):
    from tests.refresh.conftest import ATLAS_SOURCE, RECHECK, ports_at, refreshes
    from tests.refresh.test_refresh import waited

    _, sessions = risk_db
    model = ScriptedSpecialists()
    opening = Paged(discovery=[traded(SPOT), payment()])
    first = await BoundedPaperRun(
        stack_for(
            sessions,
            full_acquiring_settings(),
            now,
            ports=ports_at(now, model, market_http=opening.transport()),
        )
    ).execute()
    await waited(first, sessions)

    later = now + RECHECK
    provider = Paged(targeted=[traded(SPOT * Decimal("1.20")), payment()])
    summary = await BoundedPaperRun(
        stack_for(
            sessions,
            robinhood_run(),
            later,
            ports=ports_at(later, model, market_http=provider.transport()),
        )
    ).execute()

    case = await traded_case(sessions)
    progress = next(item for item in summary.cases if item.trade_case_id == case)
    assert refreshes(progress).get(ATLAS_SOURCE) == "ORDERED", progress
    assert [
        (item.ready, item.provider_requests, item.network_resolution_cache)
        for item in progress.market_refreshes
    ] == [(True, 1, 1), (True, 1, 1)]
    assert provider.network_requests == 3, "only the run-start acquisition scanned"


async def test_run_start_validates_both_chains_and_pre_risk_needs_two_batches(risk_db, now, trace):
    _, sessions = risk_db
    model = ScriptedSpecialists()
    case = await opened(sessions, now, model)
    await seed_on(sessions, now, {"bsc": [bsc_pool()]})
    await hold_bsc(sessions, now, trace)
    later = now + timedelta(seconds=20)
    provider = Paged(targeted=[traded(SPOT * Decimal("1.20")), payment(), bsc_pool("2.1")])

    summary = await BoundedPaperRun(
        stack_for(
            sessions,
            robinhood_run(market_chains="robinhood,bsc"),
            later,
            ports=acquiring_ports(later, model, provider),
        )
    ).execute()

    progress = next(item for item in summary.cases if item.trade_case_id == case)
    (refreshed,) = progress.market_refreshes
    assert refreshed.ready, (refreshed, summary.acquisition)
    assert set(refreshed.markets) == {PAIR_ID, BSC_PAIR_ID}
    assert refreshed.provider_requests == 2
    assert (refreshed.network_resolution_cache, refreshed.network_resolution_provider) == (2, 0)
    assert provider.network_requests == 3


async def test_a_second_run_starts_with_an_empty_registry(risk_db, now, trace):
    """The same composed stack, two passes: the second validates again."""
    _, sessions = risk_db
    model = ScriptedSpecialists()
    await opened(sessions, now, model)
    later = now + timedelta(seconds=20)
    provider = Paged(targeted=[traded(SPOT * Decimal("1.20")), payment()])
    stack = stack_for(
        sessions, robinhood_run(), later, ports=acquiring_ports(later, model, provider)
    )

    await BoundedPaperRun(stack).execute()
    after_first = provider.network_requests
    await BoundedPaperRun(replace(stack)).execute()

    assert after_first == 3
    assert provider.network_requests - after_first == 3, "nothing carried over"
