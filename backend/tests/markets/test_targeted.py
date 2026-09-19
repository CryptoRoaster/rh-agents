"""The three market-layer contracts the bounded acquisition rests on.

Kept here rather than in the runner suite because they are the market layer's
own promises: a pool can be observed again by its recorded coordinates, a
recorded market can be found by identity without being claimed to be current,
and the recorder can say whether a write was its caller's.
"""

import json
from decimal import Decimal

import httpx
import pytest

from src.core.clock import FixedClock
from src.core.config import Settings
from src.markets.geckoterminal.adapter import GeckoTerminalAdapter
from src.markets.geckoterminal.errors import ConfigurationError, IdentityError
from src.markets.geckoterminal.networks import CHAINS, NetworkDirectory
from src.markets.geckoterminal.transport import GeckoTerminalTransport
from src.markets.models import PoolLocatorIdentity, PoolLocatorKind
from src.markets.recorder import MarketRecorder, record_pair_reporting
from tests.runner.provider import NETWORKS, MarketProvider, document, payment, pool, traded

POOL = "0x" + "e5" * 20
OTHER = "0x" + "ab" * 20
VENUE = "uniswap-v3"


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://test@localhost/test",
        market_provider="geckoterminal",
        market_chains="robinhood",
    )


def locator(address: str) -> PoolLocatorIdentity:
    return PoolLocatorIdentity(kind=PoolLocatorKind.CONTRACT_ADDRESS, value=address, venue=VENUE)


def adapter_for(transport, settings, now):
    return GeckoTerminalAdapter(
        transport,
        NetworkDirectory(transport, settings),
        CHAINS["robinhood"],
        settings,
        clock=FixedClock(now),
    )


async def test_a_pool_is_observed_again_by_its_recorded_locator(settings, now, market_sessions):
    """One request, addressed by coordinates, normalized by the usual pipeline."""
    provider = MarketProvider(targeted=[traded("1.25"), payment()])

    async with GeckoTerminalTransport(settings, transport=provider.transport()) as transport:
        built = adapter_for(transport, settings, now)
        pairs = await built.observe((locator(POOL),))

        assert [item.pair_id for item in pairs] == [f"robinhood:mainnet:contract_address:{POOL}"]
        assert len(provider.multi_requests) == 1
        # Addressed, never searched: the address is in the path and no filter,
        # ranking or name appears anywhere in the request.
        assert POOL in provider.multi_requests[0]

        # And the snapshot it produced is recordable through the shared binding.
        recorder = MarketRecorder(market_sessions, clock=FixedClock(now))
        written = await record_pair_reporting(built, pairs[0], recorder)
        assert written.inserted
        assert written.observation.price.value_usd == Decimal("1.25")
        assert written.observation.observed_at == now


async def test_a_pool_that_was_not_asked_about_is_refused(settings, now):
    """A provider does not get to decide which markets this system observes."""

    def handle(request):
        if request.url.path.endswith("/networks"):
            return httpx.Response(200, text=json.dumps(NETWORKS))
        # Answers with a different pool than the one in the path.
        return httpx.Response(
            200,
            text=json.dumps(
                document([pool(OTHER, base="0x" + "a1" * 20, quote="0x" + "b2" * 20, price="2")]),
                default=str,
            ),
        )

    async with GeckoTerminalTransport(settings, transport=httpx.MockTransport(handle)) as transport:
        built = adapter_for(transport, settings, now)
        with pytest.raises(IdentityError):
            await built.observe((locator(POOL),))


async def test_a_market_the_provider_omits_is_simply_absent(settings, now):
    """Unanswered is unanswered. Nothing older is offered in its place."""
    provider = MarketProvider(targeted=[traded("1.25")])

    async with GeckoTerminalTransport(settings, transport=provider.transport()) as transport:
        built = adapter_for(transport, settings, now)
        pairs = await built.observe((locator(POOL), locator(OTHER)))

    assert [item.pair_id for item in pairs] == [f"robinhood:mainnet:contract_address:{POOL}"]


async def test_more_pools_than_one_document_can_carry_are_refused(settings, now):
    """Chunking here would spend requests the caller never authorised."""
    provider = MarketProvider(targeted=[traded("1")])
    many = tuple(locator("0x" + f"{index:02x}" * 20) for index in range(1, 25))

    async with GeckoTerminalTransport(settings, transport=provider.transport()) as transport:
        built = adapter_for(transport, settings, now)
        with pytest.raises(ConfigurationError):
            await built.observe(many)

    assert provider.paths == [], "the refusal happens before any request"


async def test_nothing_asked_for_is_nothing_requested(settings, now):
    provider = MarketProvider(targeted=[traded("1")])

    async with GeckoTerminalTransport(settings, transport=provider.transport()) as transport:
        built = adapter_for(transport, settings, now)
        assert await built.observe(()) == ()

    assert provider.paths == []


# ------------------------------------------------------------------- the reader


async def test_identities_answer_for_a_market_that_has_aged_out(recorder, reader, now, trace):
    """Coordinates, not data — which is why age is not a filter here.

    A market whose last reading aged out is exactly the one that needs observing
    again. `markets` still refuses it, because that question is about current
    state and the answer really is "nothing current".
    """
    from datetime import timedelta

    from src.markets.fake import fixture_snapshot

    snapshot = fixture_snapshot(now - timedelta(hours=2), trace)
    await recorder.record(snapshot)

    identities = await reader.identities([snapshot.pair.pair_id], include_fixtures=True)

    assert [item.pair_id for item in identities] == [snapshot.pair.pair_id]
    assert identities[0].base_asset_id == snapshot.pair.base.asset_id
    assert await reader.latest(snapshot.pair.pair_id, include_fixtures=True) is None


async def test_identities_find_a_market_by_the_asset_it_prices(recorder, reader, now, trace):
    """The payment-asset lookup: the asset must be the one being priced."""
    from src.markets.fake import fixture_snapshot

    snapshot = fixture_snapshot(now, trace)
    await recorder.record(snapshot)

    by_base = await reader.identities([snapshot.pair.base.asset_id], include_fixtures=True)
    by_quote = await reader.identities([snapshot.pair.quote.asset_id], include_fixtures=True)

    assert [item.pair_id for item in by_base] == [snapshot.pair.pair_id]
    # A pair merely mentioning an asset does not price it: the recorded stream
    # is keyed by the base asset, and the quote side answers for nothing.
    assert by_quote == ()


async def test_identities_refuse_an_unbounded_question(reader):
    with pytest.raises(ValueError):
        await reader.identities(["anything"], limit=0)
    assert await reader.identities([]) == ()


# ----------------------------------------------------------------- the recorder


async def test_the_recorder_says_which_write_was_its_callers(recorder, now, trace):
    """The same event twice is one row, and only the first call wrote it."""
    from src.markets.fake import fixture_snapshot

    snapshot = fixture_snapshot(now, trace)

    first = await recorder.record_reporting(snapshot)
    second = await recorder.record_reporting(snapshot)

    assert first.inserted
    assert not second.inserted, "an event already stored is not this caller's write"
    assert first.observation == second.observation
