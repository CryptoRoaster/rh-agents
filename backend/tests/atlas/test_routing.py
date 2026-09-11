"""Chain routing and provider construction. Nothing here performs a request.

Routing is the whole reason a Robinhood token can never be answered with BSC
data, or vice versa, and the reason an unconfigured chain fails closed instead of
borrowing a source that was never verified for it.
"""

import pytest
from pydantic import SecretStr, ValidationError

from src.agents.atlas.models import AtlasSourceFailure
from src.agents.atlas.sources.blockscout import BlockscoutHolderSource
from src.agents.atlas.sources.etherscan import EtherscanContractOriginSource
from src.agents.atlas.sources.factory import holder_sources, origin_sources
from src.agents.atlas.sources.moralis import MoralisHolderSource
from src.agents.atlas.sources.routing import RoutedHolderSource, RoutedOriginSource
from src.core.config import Settings
from src.markets.models import Availability
from tests.atlas.conftest import TOKEN

DATABASE = "postgresql+asyncpg://test_user@localhost/test_database"


class RecordingSource:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def holder_facts(self, chain, token_address):
        self.calls.append((chain, token_address))
        raise AssertionError("This source must not be reached for another chain")

    async def origin_facts(self, chain, token_address):
        self.calls.append((chain, token_address))
        raise AssertionError("This source must not be reached for another chain")


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, database_url=DATABASE, **overrides)


async def test_a_chain_without_a_provider_is_never_served_by_another_chains_source():
    recording = RecordingSource()
    routed = RoutedHolderSource(sources={"robinhood": recording})
    result = await routed.holder_facts("bsc", TOKEN)
    assert result.status == Availability.UNAVAILABLE
    assert result.failure == AtlasSourceFailure.NOT_CONFIGURED
    assert recording.calls == []


async def test_origin_routing_fails_closed_the_same_way():
    recording = RecordingSource()
    routed = RoutedOriginSource(sources={"bsc": recording})
    facts = await routed.origin_facts("robinhood", TOKEN)
    assert facts.failure == AtlasSourceFailure.NOT_CONFIGURED
    assert recording.calls == []


def test_no_provider_is_constructed_by_default():
    """A fresh deployment keeps Phase 2D's fail-closed behaviour exactly."""
    configured = settings()
    assert holder_sources(configured).sources == {}
    assert origin_sources(configured).sources == {}


def test_a_credential_alone_activates_nothing():
    configured = settings(
        blockscout_api_key=SecretStr("proapi_x"),
        moralis_api_key=SecretStr("m"),
        etherscan_api_key=SecretStr("e"),
    )
    assert holder_sources(configured).sources == {}
    assert origin_sources(configured).sources == {}


def test_selecting_providers_builds_exactly_the_configured_routing():
    configured = settings(
        atlas_rh_holder_provider="blockscout",
        atlas_bsc_holder_provider="moralis",
        atlas_rh_origin_provider="blockscout",
        atlas_bsc_origin_provider="etherscan",
        blockscout_api_key=SecretStr("proapi_x"),
        moralis_api_key=SecretStr("m"),
        etherscan_api_key=SecretStr("e"),
    )
    holders = holder_sources(configured).sources
    origins = origin_sources(configured).sources
    assert isinstance(holders["robinhood"], BlockscoutHolderSource)
    assert isinstance(holders["bsc"], MoralisHolderSource)
    assert holders["robinhood"].config.chain_id == 4663
    assert isinstance(origins["bsc"], EtherscanContractOriginSource)
    assert origins["bsc"].config.chain_id == 56


@pytest.mark.parametrize(
    "selection",
    [
        {"atlas_rh_holder_provider": "blockscout"},
        {"atlas_bsc_holder_provider": "moralis"},
        {"atlas_rh_origin_provider": "blockscout"},
        {"atlas_bsc_origin_provider": "etherscan"},
    ],
)
def test_a_selected_provider_without_its_key_refuses_to_boot(selection):
    with pytest.raises(ValidationError):
        settings(**selection)


@pytest.mark.parametrize(
    "url",
    [
        "http://api.blockscout.com",
        "https://user:secret@api.blockscout.com",
        "https://api.blockscout.com/?apikey=leaked",
        "https://api.blockscout.com#fragment",
        "https://api blockscout.com",
        "not-a-url",
    ],
)
def test_a_provider_base_url_must_be_a_plain_https_origin(url):
    with pytest.raises(ValidationError):
        settings(blockscout_base_url=url)


def test_the_default_provider_origins_are_the_documented_ones():
    configured = settings()
    assert configured.blockscout_base_url == "https://api.blockscout.com"
    assert configured.moralis_base_url == "https://deep-index.moralis.io/api/v2.2"
    assert configured.etherscan_base_url == "https://api.etherscan.io"
