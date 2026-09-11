from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.markets.fake import InMemoryProvider, fixture_snapshot
from src.markets.models import Availability, MarketCandidate, MarketSnapshot
from src.markets.providers import normalize_snapshot


def test_exact_decimal_and_immutable_roundtrip(observation):
    serialized = observation.model_dump_json()
    parsed = MarketSnapshot.model_validate_json(serialized)
    assert parsed == observation
    assert parsed.price.value_usd == Decimal("2345.123456789012345678")
    assert '"2345.123456789012345678"' in serialized
    with pytest.raises(ValidationError):
        parsed.price.value_usd = Decimal("1")


@pytest.mark.parametrize("value", [1.5, True, "NaN", "Infinity", "-1", "0", "bad", "1e-1001"])
def test_malformed_price_rejected(observation, value):
    data = observation.model_dump()
    data["price"]["value_usd"] = value
    with pytest.raises(ValidationError):
        normalize_snapshot(data, provider=observation.provider, is_fixture=True)


@pytest.mark.parametrize("section", ["price", "liquidity", "volume"])
@pytest.mark.parametrize("status", [Availability.UNKNOWN, Availability.UNAVAILABLE])
def test_missing_values_remain_explicit_null(observation, section, status):
    data = observation.model_dump()
    data[section]["status"] = status
    data[section]["value_usd"] = None
    parsed = MarketSnapshot.model_validate(data)
    assert getattr(parsed, section).value_usd is None
    assert getattr(parsed, section).status == status
    assert '"value_usd":null' in parsed.model_dump_json()


@pytest.mark.parametrize(
    "status,value", [("AVAILABLE", None), ("UNKNOWN", "0"), ("UNAVAILABLE", "1")]
)
def test_availability_and_value_must_agree(observation, status, value):
    data = observation.model_dump()
    data["liquidity"].update(status=status, value_usd=value)
    with pytest.raises(ValidationError):
        MarketSnapshot.model_validate(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("asset_id", "WETH"),
        ("asset_id", "solana:mainnet:WETH"),
        ("network", "sepolia"),
        ("chain", "ethereum:mainnet"),
    ],
)
def test_chain_network_qualified_identity(observation, field, value):
    data = observation.model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        MarketSnapshot.model_validate(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "other"),
        ("asset_id", "ethereum:mainnet:other"),
        ("is_fixture", False),
        ("correlation_id", "00000000-0000-0000-0000-000000000002"),
    ],
)
def test_nested_provenance_cannot_disagree(observation, field, value):
    data = observation.model_dump()
    data["liquidity"][field] = value
    with pytest.raises(ValidationError, match="provenance"):
        MarketSnapshot.model_validate(data)


def test_adapter_provenance_is_bound(observation):
    for provider, fixture in [("other", True), (observation.provider, False)]:
        with pytest.raises(ValueError, match="provenance"):
            normalize_snapshot(observation.model_dump(), provider=provider, is_fixture=fixture)


def test_freshness_boundaries(observation, now):
    max_age = timedelta(seconds=60)
    assert observation.is_valid_at(now, max_age)
    assert observation.is_valid_at(now + max_age, max_age)
    assert not observation.is_valid_at(now + max_age + timedelta(microseconds=1), max_age)
    assert not observation.is_valid_at(now - timedelta(microseconds=1), max_age)
    with pytest.raises(ValueError):
        observation.age(now.replace(tzinfo=None))


def test_old_nested_data_is_stale_even_with_recent_envelope(observation, now):
    data = observation.model_dump()
    data["liquidity"]["observed_at"] = now - timedelta(seconds=61)
    parsed = MarketSnapshot.model_validate(data)
    assert not parsed.is_valid_at(now, timedelta(seconds=60))
    data["liquidity"]["observed_at"] = now + timedelta(seconds=1)
    with pytest.raises(ValidationError):
        MarketSnapshot.model_validate(data)


async def test_fake_provider_is_deterministic_and_explicit(observation, now, trace):
    assert fixture_snapshot(now, trace) == observation
    provider = InMemoryProvider((observation,))
    (pair,) = await provider.discover()
    assert await provider.snapshot(pair) == observation
    assert await provider.price(pair) == observation.price
    assert await provider.liquidity(pair) == observation.liquidity
    assert await provider.volume(pair) == observation.volume
    assert provider.is_fixture is True
    candidate = MarketCandidate.from_snapshot(observation)
    assert candidate == MarketCandidate.from_snapshot(observation)
    assert candidate.snapshot_id == observation.id
    assert candidate.asset_id == observation.asset_id
    assert candidate.is_fixture
