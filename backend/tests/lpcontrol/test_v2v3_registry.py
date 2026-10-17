"""Issue #102: nothing V2/V3 can pass a BUY today.

The production deployment registry and the locker adapters are empty until a
factory, pair template, position manager or locker has been verified on-chain
under a separate approval. `lp_control_view` is unchanged: every 20-byte pool
stays UNSUPPORTED `LP_CONTROL_SOURCE_ABSENT`, and the risk engine refuses a
PASS on any basis but the V4 pool census -- including the V2/V3 census names.
"""

import pytest

from src.agents.atlas.lpcontrol.custody import DEFAULT_LOCKERS
from src.agents.atlas.lpcontrol.decision import (
    LP_CONTROL_V2_LP_TOKEN_CENSUS,
    LP_CONTROL_V3_POSITION_CENSUS,
)
from src.agents.atlas.lpcontrol.registry import (
    LP_DEPLOYMENTS,
    LpDeploymentRegistry,
    V2Deployment,
)
from src.core.models import LpControlStatus, RiskLimits, RiskOutcome
from src.orchestration.riskrequest.service import lp_control_view
from src.orchestration.workflow.models import OnchainPayload
from src.risk.engine import evaluate, lp_control_refusal
from tests.lpcontrol.test_red_regression import TWENTY_BYTE_VENUES
from tests.lpcontrol.v2v3_fixtures import FIXTURE_CHAIN, FIXTURE_CHAIN_ID, V2_FACTORY
from tests.riskdata.conftest import onchain_payload, recorded_snapshot


def test_the_production_registry_is_empty() -> None:
    # Adding an entry requires on-chain verification and a deliberate change
    # here; see docs/lp-control-v2-v3.md.
    assert LP_DEPLOYMENTS.v2 == ()
    assert LP_DEPLOYMENTS.v3 == ()
    assert DEFAULT_LOCKERS == ()


def test_a_registry_rejects_duplicates_and_conflicting_chain_ids() -> None:
    entry = V2Deployment(
        chain=FIXTURE_CHAIN,
        chain_id=FIXTURE_CHAIN_ID,
        venue="fixture-v2",
        factory=V2_FACTORY,
        pair_code_hash="0x" + "c2" * 32,
        start_block=1,
    )
    with pytest.raises(ValueError, match="once"):
        LpDeploymentRegistry(v2=(entry, entry))
    other = V2Deployment(**{**entry.__dict__, "chain_id": 1, "factory": "0x" + "f9" * 20})
    with pytest.raises(ValueError, match="one chain id"):
        LpDeploymentRegistry(v2=(entry, other))
    with pytest.raises(ValueError, match="lowercase"):
        V2Deployment(**{**entry.__dict__, "factory": V2_FACTORY.upper().replace("0X", "0x")})


@pytest.mark.parametrize("venue", TWENTY_BYTE_VENUES)
def test_lp_control_view_still_reports_every_twenty_byte_pool_unsupported(now, venue) -> None:
    snapshot = recorded_snapshot(now, venue=venue)
    onchain = OnchainPayload.model_validate(onchain_payload(now))
    found = lp_control_view(snapshot=snapshot, onchain=onchain)
    assert (found.status, found.detail, found.pool_locator_kind) == (
        LpControlStatus.UNSUPPORTED,
        "LP_CONTROL_SOURCE_ABSENT",
        "CONTRACT_ADDRESS",
    )


@pytest.mark.parametrize("basis", [LP_CONTROL_V2_LP_TOKEN_CENSUS, LP_CONTROL_V3_POSITION_CENSUS])
def test_the_engine_refuses_a_v2_v3_basis_pass(intent, market, context, now, basis) -> None:
    assert market.lp_control is not None
    forged = market.model_copy(
        update={
            "lp_control": market.lp_control.model_copy(
                update={
                    "status": LpControlStatus.PASS,
                    "pool_locator_kind": "CONTRACT_ADDRESS",
                    "pool_id": "0x" + "b2" * 20,
                    "basis": basis,
                }
            )
        }
    )
    assert lp_control_refusal(forged) == "LP_CONTROL_EVIDENCE_INCONSISTENT"
    assert evaluate(intent, forged, context, RiskLimits(), now=now).outcome is RiskOutcome.REJECT
