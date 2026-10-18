"""An exit's fresh on-chain read may establish nothing about a domain.

ATLAS records `UNKNOWN` for a contract or holder domain it could not establish
(stale snapshot, source skew, unaccepted holder coverage) while still carrying
the holder figures it measured. The sale must then be judged -- and refused --
by SENTINEL as unknown, a typed outcome. Before, building the exit's market
view raised `KeyError`, and the exit phase does not catch that: one such
position ended the phase and every later position's exit in the same run.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.core.models import RiskLimits, RiskOutcome, SafetyStatus, Side
from src.orchestration.riskrequest.service import market_view
from src.orchestration.sizing.context import base_asset_metadata, reference_price
from src.risk.engine import evaluate
from tests.riskdata.conftest import (
    BASE_ASSET,
    configured_costs,
    onchain_payload,
    recorded_snapshot,
)


@pytest.mark.parametrize(
    ("domain", "reason"),
    [("holder_integrity", "HOLDERS_UNKNOWN"), ("contract_integrity", "TOKEN_UNKNOWN")],
)
def test_an_unknown_domain_on_an_exit_read_is_judged_not_raised(
    now, intent, context, domain, reason
) -> None:
    snapshot = recorded_snapshot(now, age=timedelta(seconds=5))
    payload = onchain_payload(now).model_copy(update={domain: "UNKNOWN"})
    assert payload.intelligence is not None and payload.intelligence.holders is not None
    price = reference_price(snapshot)
    metadata = base_asset_metadata(snapshot)
    assert price is not None and metadata is not None
    view = market_view(
        base_asset_id=BASE_ASSET,
        price=price,
        base_asset=metadata,
        snapshot=snapshot,
        onchain=payload,
        holders=payload.intelligence.holders,
        routing=SafetyStatus.PASS,
        costs=configured_costs(),
        correlation_id=uuid4(),
        identity_key="exit-unknown",
        side=Side.SELL,
    )
    sale = intent.model_copy(
        update={"asset_id": BASE_ASSET, "side": Side.SELL, "quantity": Decimal("1")}
    )
    held = context.model_copy(update={"position_quantity": Decimal("1")})
    decision = evaluate(sale, view, held, RiskLimits(), now=now)
    assert decision.outcome is RiskOutcome.REJECT
    assert reason in decision.reason_codes
