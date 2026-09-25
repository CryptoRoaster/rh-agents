"""VECTOR reads recorded market facts at market precision; its own levels stay bounded.

Two precision domains, kept apart. What the market recorded — the current price,
liquidity and volume, and every OHLCV value — is copied exactly, however many
decimal places the provider reported. What VECTOR proposes — entry, invalidation,
targets, trigger levels — keeps its 18-place contract, so a model never gains
precision because a market reported more.
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.agents.vector.context import (
    build_setup,
    structure_document,
    vector_input_digest,
)
from src.agents.vector.models import TriggerCondition, VectorSetupProposal
from src.agents.vector.validation import trigger_for
from src.core.numbers import canonical_decimal
from tests.vector.conftest import breakout, history_for, proposal_payload
from tests.vector.test_context import read, snapshot_for

# Synthetic, 23 decimal places: beyond any 18-place ledger type.
WIDE_PRICE = Decimal("1.00000000000000000000001")
WIDE_BAR_PRICE = Decimal("0.12345678901234567890123")


def places(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    assert isinstance(exponent, int)
    return max(0, -exponent)


async def test_current_snapshot_is_copied_exactly(now) -> None:
    context = await read(now, snapshot=snapshot_for(now, price=WIDE_PRICE))
    assert context.market.price.value_usd == WIDE_PRICE
    assert context.market.price.value_usd.as_tuple() == WIDE_PRICE.as_tuple()
    assert context.latest_price == WIDE_PRICE


async def test_history_bars_are_copied_exactly(now) -> None:
    history = history_for(now, price=WIDE_BAR_PRICE)
    assert any(places(bar.close) > 18 for bar in history.bars)
    context = await read(now, history=history)

    copied = context.market.structure.bars
    assert len(copied) == len(history.bars)
    for source, bar in zip(history.bars, copied, strict=True):
        for field in ("open", "high", "low", "close", "volume"):
            assert getattr(bar, field) == getattr(source, field)
            assert getattr(bar, field).as_tuple() == getattr(source, field).as_tuple()


async def test_structure_document_is_exact_and_canonical(now) -> None:
    history = history_for(now, price=WIDE_BAR_PRICE)
    context = await read(now, history=history)
    document = structure_document(context.market.structure)

    bars = document["bars"]
    assert isinstance(bars, list)
    for source, rendered in zip(history.bars, bars, strict=True):
        assert rendered["close"] == canonical_decimal(source.close)
        assert "E" not in rendered["close"] and "e" not in rendered["close"]
        # Re-parsing the text gives back the recorded value: nothing was rounded.
        assert Decimal(rendered["close"]) == source.close
    assert document["observed_range_low"] == canonical_decimal(context.market.structure.range_low)


async def test_input_digest_is_deterministic_for_wide_inputs(now) -> None:
    snapshot = snapshot_for(now, price=WIDE_PRICE)
    history = history_for(now, price=WIDE_BAR_PRICE)
    first = await read(now, snapshot=snapshot, history=history)
    second = await read(now, snapshot=snapshot, history=history)
    assert vector_input_digest(first) == vector_input_digest(second)


async def test_a_setup_records_the_wide_reference_price_exactly(now) -> None:
    context = await read(now, snapshot=snapshot_for(now, price=WIDE_PRICE))
    proposal = breakout(now)
    setup = build_setup(proposal, trigger_for(proposal, context), context, "0" * 64)
    assert setup.reference_price == WIDE_PRICE
    # The levels themselves are the proposal's, still 18-place values.
    assert all(places(level) <= 18 for level in (setup.entry_low, setup.entry_high))


def test_proposal_levels_keep_the_bounded_output_contract(now) -> None:
    """The output schema did not widen: a 19-place entry level is still refused."""
    too_precise = "1.1000000000000000001"
    valid = proposal_payload(now)
    VectorSetupProposal.model_validate(valid)
    for field in ("entry_low", "entry_high", "invalidation_price"):
        with pytest.raises(ValidationError) as refused:
            VectorSetupProposal.model_validate({**valid, field: too_precise})
        assert any(e["type"] == "decimal_max_places" for e in refused.value.errors())
    with pytest.raises(ValidationError) as refused:
        VectorSetupProposal.model_validate({**valid, "targets": [too_precise]})
    assert any(e["type"] == "decimal_max_places" for e in refused.value.errors())


def test_trigger_levels_keep_the_bounded_contract(now) -> None:
    from datetime import timedelta

    from src.agents.vector.models import TriggerType

    with pytest.raises(ValidationError):
        TriggerCondition(
            type=TriggerType.PRICE_GTE,
            reference_price=Decimal("1.1000000000000000001"),
            valid_from=now,
            expires_at=now + timedelta(hours=1),
        )


def test_stored_evidence_keeps_recorded_facts_wide_and_levels_bounded(now) -> None:
    """The workflow payload draws the same line as the models it is built from."""
    from src.orchestration.workflow.models import RecordedBar, TradeSetupTrigger

    bar = RecordedBar(
        opened_at=now,
        open=WIDE_BAR_PRICE,
        high=WIDE_BAR_PRICE,
        low=WIDE_BAR_PRICE,
        close=WIDE_BAR_PRICE,
        volume=Decimal("0.00000000000000000000042"),
    )
    assert bar.close == WIDE_BAR_PRICE
    with pytest.raises(ValidationError):
        TradeSetupTrigger.model_validate(
            {
                "type": "PRICE_GTE",
                "price_basis": "USD_PER_BASE_UNIT",
                "reference_price": "1.1000000000000000001",
                "valid_from": now.isoformat(),
                "expires_at": now.isoformat(),
            }
        )
