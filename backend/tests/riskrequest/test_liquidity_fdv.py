"""A purchase's market view carries the supply its FDV is judged on; a sale's does not."""

from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from src.core.models import AgentRole, Side
from src.orchestration.riskrequest.service import risk_market
from src.orchestration.sizing.context import base_asset_metadata, reference_price
from src.orchestration.workflow.models import EvidenceType, HolderDistributionFacts
from tests.anchor.conftest import evidence_envelope
from tests.riskdata.conftest import (
    BASE_ASSET,
    TOTAL_SUPPLY,
    anchor_payload,
    configured_costs,
    holder_block,
    onchain_payload,
    recorded_snapshot,
)

CORRELATION = UUID("00000000-0000-4000-8000-0000000fd701")


def view(now, side):
    snapshot = recorded_snapshot(now)
    price = reference_price(snapshot)
    metadata = base_asset_metadata(snapshot)
    assert price is not None and metadata is not None
    return risk_market(
        base_asset_id=BASE_ASSET,
        price=price,
        base_asset=metadata,
        snapshot=snapshot,
        onchain=evidence_envelope(now, EvidenceType.ONCHAIN, AgentRole.ATLAS, onchain_payload(now)),
        anchor=evidence_envelope(
            now,
            EvidenceType.LIQUIDITY_EXECUTION,
            AgentRole.ANCHOR,
            anchor_payload(uuid4(), uuid4()),
        ),
        costs=configured_costs(),
        correlation_id=CORRELATION,
        identity_key=f"liquidity-fdv-{side.value}",
        side=side,
    )


def test_a_purchase_carries_the_contract_supply(now) -> None:
    token = view(now, Side.BUY).token
    assert token.total_supply_raw == TOTAL_SUPPLY
    # Its scale from the contract read, beside the provider's decimals.
    assert token.total_supply_decimals == 18
    assert token.decimals == 18


def test_a_purchase_says_whether_its_price_and_pool_were_rounded(now) -> None:
    market = view(now, Side.BUY)
    assert isinstance(market.price_exact, bool)
    assert isinstance(market.liquidity.liquidity_exact, bool)


def test_a_sale_keeps_its_shape(now) -> None:
    market = view(now, Side.SELL)
    assert market.token.total_supply_raw is None
    assert market.token.total_supply_decimals is None
    dumped = market.model_dump(mode="json")["token"]
    assert "total_supply_raw" not in dumped
    assert "total_supply_decimals" not in dumped
    assert market.price_exact is None
    assert market.liquidity.liquidity_exact is None
    assert "price_exact" not in market.model_dump(mode="json")
    assert "liquidity_exact" not in market.model_dump(mode="json")["liquidity"]


def test_holder_evidence_without_contract_decimals_replays_unchanged(now) -> None:
    """Evidence stored before the decimals were carried keeps its bytes."""
    legacy = holder_block(now).model_copy(update={"total_supply_decimals": None})
    dumped = legacy.model_dump(mode="json")
    assert "total_supply_decimals" not in dumped
    assert HolderDistributionFacts.model_validate(dumped).total_supply_decimals is None


@pytest.mark.parametrize("bad", [-1, 37, "18", 18.0])
def test_holder_evidence_cannot_carry_an_impossible_scale(now, bad) -> None:
    fields = holder_block(now).model_dump()
    with pytest.raises(ValidationError):
        HolderDistributionFacts(**{**fields, "total_supply_decimals": bad})
