from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.agents.orbit.context import (
    OrbitContextUnavailable,
    orbit_input_digest,
    reasoning_payload,
)
from src.agents.orbit.models import ObservedMeasurement, OrbitTaskInput
from src.markets.models import Availability
from tests.orbit.conftest import (
    DISCOVERY_FLOOR,
    MAX_INPUT_AGE,
    reader_for,
    unknown_snapshot,
    valued_snapshot,
)


async def build(snapshot, now, trace, **kwargs):
    reader = reader_for(snapshot, now, trace, **kwargs)
    return await reader.candidate_context(reader.cases.trade_case.id, uuid4())


async def test_context_carries_only_the_candidate_under_assessment(task_input, snapshot):
    candidate = task_input.candidate
    assert candidate.snapshot_id == snapshot.id
    assert candidate.pair_id == snapshot.pair.pair_id
    assert candidate.chain == snapshot.chain
    assert candidate.base_symbol == "WETH"
    assert task_input.discovery_liquidity_floor_usd == DISCOVERY_FLOOR
    # No other role's evidence, no risk outcome and no case status is reachable.
    fields = set(type(candidate).model_fields)
    assert not any(
        word in name
        for name in fields
        for word in ("risk", "sentinel", "status_", "evidence", "session", "key")
    )


async def test_money_never_passes_through_a_float(task_input):
    payload = reasoning_payload(task_input)
    observation = payload["market_observation"]
    assert isinstance(observation, dict)
    price = observation["price"]
    assert isinstance(price, dict)
    assert isinstance(price["value_usd"], str)
    assert Decimal(price["value_usd"]) == Decimal("2345.123456789012345678")
    assert isinstance(observation["discovery_liquidity_floor_usd"], str)


# ---------------------------------------------------------------- zero vs unknown


async def test_available_zero_and_unknown_are_different_inputs(snapshot, now, trace):
    zero = await build(valued_snapshot(snapshot, "liquidity", Decimal("0")), now, trace)
    unknown = await build(unknown_snapshot(snapshot, "liquidity"), now, trace)

    assert zero.candidate.liquidity.status == Availability.AVAILABLE
    assert zero.candidate.liquidity.value_usd == Decimal("0")
    assert unknown.candidate.liquidity.status == Availability.UNKNOWN
    assert unknown.candidate.liquidity.value_usd is None

    # The model is never told that an unobserved value is zero.
    zero_payload = reasoning_payload(zero)["market_observation"]
    unknown_payload = reasoning_payload(unknown)["market_observation"]
    assert isinstance(zero_payload, dict) and isinstance(unknown_payload, dict)
    assert zero_payload["liquidity"]["value_usd"] == "0"
    assert unknown_payload["liquidity"]["value_usd"] is None
    assert orbit_input_digest(zero) != orbit_input_digest(unknown)


@pytest.mark.parametrize("field", ["price", "liquidity", "volume"])
@pytest.mark.parametrize("status", [Availability.UNKNOWN, Availability.UNAVAILABLE])
async def test_every_measurement_preserves_its_absence(snapshot, now, trace, field, status):
    absent = await build(unknown_snapshot(snapshot, field, status), now, trace)
    measurement = getattr(absent.candidate, field)
    assert measurement.status == status
    assert measurement.value_usd is None


def test_measurement_cannot_claim_a_value_it_does_not_have():
    for status, value in ((Availability.UNKNOWN, Decimal("1")), (Availability.AVAILABLE, None)):
        with pytest.raises(ValidationError):
            ObservedMeasurement(
                observation_id=uuid4(),
                status=status,
                value_usd=value,
                observed_at=datetime.now(UTC),
            )


# ------------------------------------------------------------------------ digest


async def test_digest_is_stable_for_identical_input(snapshot, now, trace):
    first = await build(snapshot, now, trace)
    second = await build(snapshot, now, trace)
    assert orbit_input_digest(first) == orbit_input_digest(second)


async def test_digest_ignores_only_the_relative_age(snapshot, now, trace):
    fresh = await build(snapshot, now, trace)
    later = await build(snapshot, now, trace, clock_at=now + timedelta(minutes=5))
    assert later.candidate.age_seconds > fresh.candidate.age_seconds
    # Age is relative to reading time; the observation itself is unchanged.
    assert orbit_input_digest(later) == orbit_input_digest(fresh)


@pytest.mark.parametrize(
    "field,value",
    [("price", Decimal("1")), ("liquidity", Decimal("999")), ("volume", Decimal("7"))],
)
async def test_digest_changes_when_market_data_changes(snapshot, now, trace, field, value):
    baseline = await build(snapshot, now, trace)
    changed = await build(valued_snapshot(snapshot, field, value), now, trace)
    assert orbit_input_digest(changed) != orbit_input_digest(baseline)


async def test_digest_changes_with_the_discovery_floor(snapshot, now, trace):
    baseline = await build(snapshot, now, trace)
    other = baseline.model_copy(update={"discovery_liquidity_floor_usd": Decimal("1")})
    assert orbit_input_digest(other) != orbit_input_digest(baseline)


# -------------------------------------------------------------------- freshness


async def test_stale_observation_is_refused_before_any_model_call(snapshot, now, trace):
    with pytest.raises(OrbitContextUnavailable) as caught:
        await build(snapshot, now, trace, clock_at=now + MAX_INPUT_AGE + timedelta(seconds=1))
    assert caught.value.reason_code == "MARKET_OBSERVATION_TOO_STALE"


async def test_observation_exactly_at_the_limit_is_still_usable(snapshot, now, trace):
    built = await build(snapshot, now, trace, clock_at=now + MAX_INPUT_AGE)
    assert built.candidate.age_seconds == int(MAX_INPUT_AGE.total_seconds())


async def test_missing_and_future_observations_are_refused(snapshot, now, trace):
    reader = reader_for(snapshot, now, trace)
    reader.markets.snapshot = None
    with pytest.raises(OrbitContextUnavailable) as caught:
        await reader.candidate_context(reader.cases.trade_case.id, uuid4())
    assert caught.value.reason_code == "MARKET_OBSERVATION_MISSING"

    with pytest.raises(OrbitContextUnavailable) as caught:
        await build(snapshot, now, trace, clock_at=now - timedelta(minutes=1))
    assert caught.value.reason_code == "MARKET_OBSERVATION_IN_FUTURE"


async def test_identity_mismatch_is_refused(snapshot, now, trace):
    reader = reader_for(snapshot, now, trace)
    other = snapshot.model_copy(
        update={"pair": snapshot.pair.model_copy(update={"pair_id": "ethereum:mainnet:other"})}
    )
    reader.markets.snapshot = other
    with pytest.raises(OrbitContextUnavailable) as caught:
        await reader.candidate_context(reader.cases.trade_case.id, uuid4())
    assert caught.value.reason_code == "MARKET_IDENTITY_MISMATCH"


async def test_task_input_is_frozen(task_input):
    assert isinstance(task_input, OrbitTaskInput)
    with pytest.raises(ValidationError):
        task_input.candidate = None


async def test_decimals_are_canonical_and_never_scientific(snapshot, now, trace):
    from src.agents.orbit.context import observation_document

    tiny = await build(
        valued_snapshot(snapshot, "liquidity", Decimal("0.000000000000000001")), now, trace
    )
    document = observation_document(tiny)
    liquidity = document["liquidity"]
    assert isinstance(liquidity, dict)
    # str(Decimal) would render this as "1E-18", which is both harder to read and
    # a needless ambiguity in the document the model is shown.
    assert liquidity["value_usd"] == "0.000000000000000001"
    numeric = [liquidity["value_usd"], document["discovery_liquidity_floor_usd"]]
    assert all(isinstance(value, str) and "E" not in value for value in numeric)


async def test_equal_values_written_differently_hash_the_same(snapshot, now, trace):
    padded = await build(valued_snapshot(snapshot, "liquidity", Decimal("1.10")), now, trace)
    plain = await build(valued_snapshot(snapshot, "liquidity", Decimal("1.1")), now, trace)
    assert padded.candidate.liquidity.value_usd == plain.candidate.liquidity.value_usd
    assert orbit_input_digest(padded) == orbit_input_digest(plain)


async def test_the_digest_fingerprints_exactly_the_document_shown(task_input):
    import json
    from hashlib import sha256

    from src.agents.orbit.context import observation_document

    document = observation_document(task_input)
    expected = sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert orbit_input_digest(task_input) == expected
    # The provider sees that same document, plus the relative age for context.
    shown = reasoning_payload(task_input)["market_observation"]
    assert isinstance(shown, dict)
    assert {key: shown[key] for key in document} == document
