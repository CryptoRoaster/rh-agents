"""Issue #98: the LP-control evidence names the traded market's own pool.

A PASS rests on the census entry whose pool id equals the market's locator.
ATLAS only admits pools whose key contains the token and only reads the
verified PoolManager, but the decision must not rest on that producer alone:
the market pool's currencies must contain the market's own token, and a market
that names its PoolManager must name the verified one. Each is checked where
the decision is made.
"""

from src.core.models import LpControlStatus
from src.markets.models import Availability, PoolLocator, PoolLocatorKind
from src.orchestration.riskrequest.service import lp_control_view
from src.orchestration.workflow.models import OnchainPayload
from tests.atlas.v4.chain import POOL_MANAGER
from tests.lpcontrol.test_census_basis import launch, with_census
from tests.lpcontrol.test_lp_control import recorded

FOREIGN = "0x" + "11" * 20


def market(now, pool_id: str, *, manager: str | None = None):
    snapshot = recorded(now, pool_id=pool_id)
    locator = PoolLocator(
        kind=PoolLocatorKind.BYTES32_POOL_ID,
        value=pool_id,
        venue="uniswap-v4",
        pool_manager_address=manager,
        manager_status=Availability.AVAILABLE if manager else Availability.UNKNOWN,
    )
    pair = snapshot.pair.model_copy(update={"pool_locator": locator})
    return snapshot.model_copy(update={"pair": pair})


def with_market_pool(payload: OnchainPayload, pool_id: str, **changes: object) -> OnchainPayload:
    assert payload.intelligence is not None and payload.intelligence.pool_control is not None
    pools = tuple(
        item.model_copy(update=changes) if item.pool_id == pool_id else item
        for item in payload.intelligence.pool_control.pools
    )
    return with_census(payload, pools=pools)


async def test_a_market_pool_without_the_markets_token_is_unknown(now) -> None:
    payload, pool_id = await launch(now)
    assert lp_control_view(snapshot=market(now, pool_id), onchain=payload)[0] is (
        LpControlStatus.PASS
    )
    # Same pool id, but the census entry pairs two other currencies.
    other = with_market_pool(payload, pool_id, currency0="0x" + "00" * 20, currency1=FOREIGN)
    found = lp_control_view(snapshot=market(now, pool_id), onchain=other)
    assert found[:2] == (LpControlStatus.UNKNOWN, "V4_MARKET_POOL_TOKEN_MISMATCH")


async def test_a_market_naming_another_pool_manager_is_unknown(now) -> None:
    payload, pool_id = await launch(now)
    found = lp_control_view(snapshot=market(now, pool_id, manager=FOREIGN), onchain=payload)
    assert found[:2] == (LpControlStatus.UNKNOWN, "V4_MARKET_POOL_MANAGER_MISMATCH")


async def test_a_market_naming_the_verified_pool_manager_passes(now, intent, context) -> None:
    payload, pool_id = await launch(now)
    found = lp_control_view(snapshot=market(now, pool_id, manager=POOL_MANAGER), onchain=payload)
    assert found[0] is LpControlStatus.PASS, found.detail
