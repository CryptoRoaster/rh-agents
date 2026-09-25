"""ORBIT reads recorded market facts at the precision the market layer recorded.

A real BSC price carried 23 decimal places. The market contract accepts that on
purpose; ORBIT used to narrow it to 18 places while copying it into its own view
and failed before any reasoning call. These tests pin the exact copy.
"""

from decimal import Decimal
from uuid import uuid4

from src.agents.orbit.context import observation_document, orbit_input_digest
from src.core.numbers import canonical_decimal
from src.markets.models import MarketSnapshot
from tests.orbit.conftest import reader_for, valued_snapshot

# Synthetic: 23 decimal places, more than any 18-place ledger type can hold.
WIDE_PRICE = Decimal("0.12345678901234567890123")
WIDE_LIQUIDITY = Decimal("30000.0000000000000000000001")
WIDE_VOLUME = Decimal("0.00000000000000000000042")


def wide_snapshot(snapshot: MarketSnapshot) -> MarketSnapshot:
    widened = valued_snapshot(snapshot, "price", WIDE_PRICE)
    widened = valued_snapshot(widened, "liquidity", WIDE_LIQUIDITY)
    widened = valued_snapshot(widened, "volume", WIDE_VOLUME)
    # The market contract itself must accept every one of these values.
    return MarketSnapshot.model_validate(widened.model_dump())


async def test_market_layer_accepts_more_than_18_places(snapshot, now, trace) -> None:
    widened = wide_snapshot(snapshot)
    assert widened.price.value_usd == WIDE_PRICE
    assert widened.liquidity.value_usd == WIDE_LIQUIDITY


async def test_candidate_context_copies_wide_market_facts_exactly(snapshot, now, trace) -> None:
    widened = wide_snapshot(snapshot)
    reader = reader_for(widened, now, trace)
    task_input = await reader.candidate_context(reader.cases.trade_case.id, uuid4())

    candidate = task_input.candidate
    assert candidate.price.value_usd == WIDE_PRICE
    assert candidate.liquidity.value_usd == WIDE_LIQUIDITY
    assert candidate.volume.value_usd == WIDE_VOLUME
    # Same digits, same exponent: nothing was quantized on the way in.
    assert candidate.price.value_usd.as_tuple() == WIDE_PRICE.as_tuple()
    for value in (candidate.price.value_usd, candidate.liquidity.value_usd):
        assert isinstance(value, Decimal)


async def test_observation_document_carries_the_exact_canonical_value(snapshot, now, trace) -> None:
    widened = wide_snapshot(snapshot)
    reader = reader_for(widened, now, trace)
    task_input = await reader.candidate_context(reader.cases.trade_case.id, uuid4())

    document = observation_document(task_input)
    assert document["price"]["value_usd"] == canonical_decimal(WIDE_PRICE)  # type: ignore[index]
    assert document["price"]["value_usd"] == "0.12345678901234567890123"  # type: ignore[index]
    assert document["volume"]["value_usd"] == "0.00000000000000000000042"  # type: ignore[index]
    assert orbit_input_digest(task_input) == orbit_input_digest(task_input)


async def test_policy_floor_keeps_its_bounded_contract(snapshot, now, trace) -> None:
    """The discovery floor is configuration, not a market fact; it stays 18-place."""
    import pytest
    from pydantic import ValidationError

    from src.agents.orbit.models import OrbitTaskInput

    reader = reader_for(snapshot, now, trace)
    task_input = await reader.candidate_context(reader.cases.trade_case.id, uuid4())
    too_precise = task_input.model_dump()
    too_precise["discovery_liquidity_floor_usd"] = Decimal("1.0000000000000000001")
    with pytest.raises(ValidationError):
        OrbitTaskInput.model_validate(too_precise)
