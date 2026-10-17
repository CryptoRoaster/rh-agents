"""Issue #92, pre-merge review N2: LP-control evidence is as old as its census.

The census is read at the ATLAS chain block. ATLAS accepts that block up to
five minutes apart from the holder observation, while SENTINEL allows safety
data thirty seconds. Dated by the holder observation, a census minutes old
would pass as fresh. So the LP-control evidence carries the census block's own
chain time, and SENTINEL judges that.
"""

from datetime import timedelta

from src.agents.atlas.handler import onchain_payload as atlas_payload
from src.agents.atlas.models import HolderCompleteness, HolderObservationBasis, HolderSourceRow
from src.agents.atlas.policy import evaluate_snapshot
from src.core.models import LpControlStatus, RiskLimits, RiskOutcome
from src.orchestration.riskrequest.service import too_old_for
from src.orchestration.workflow.models import OnchainPayload
from tests.atlas.v4.chain import POOL_MANAGER
from tests.atlas.v4.scenarios import build, official_launch
from tests.lpcontrol.test_lp_control import sentinel


async def launch(now, *, census_age: timedelta) -> tuple[OnchainPayload, str]:
    """A complete, verified launch whose chain block is ``census_age`` old.

    The holder list is a current-state answer received now, so ATLAS accepts
    the block as long as it is within its five-minute source skew.
    """
    chain, pool, rows = official_launch()
    chain.extra_pool_balance = 0
    rows = (
        HolderSourceRow(address=POOL_MANAGER, balance_raw=chain.pool_balance(chain.head)),
        *rows[1:],
    )
    snapshot = await build(
        now,
        chain,
        rows=rows,
        completeness=HolderCompleteness.COMPLETE,
        supply=sum(row.balance_raw for row in rows),
        holder_count=len(rows),
        block_timestamp=now - census_age,
        observation_basis=HolderObservationBasis.RESPONSE_TIME,
    )
    decision = evaluate_snapshot(snapshot, now)
    assert not decision.data_gaps, decision.data_gaps
    return atlas_payload(snapshot, decision), pool.id


async def test_a_census_minutes_old_is_not_dated_by_the_holder_observation(
    now, intent, context
) -> None:
    payload, pool_id = await launch(now, census_age=timedelta(minutes=4))
    market, decision = sentinel(now, intent, context, payload, pool_id=pool_id)
    assert market.lp_control is not None
    assert market.lp_control.status is LpControlStatus.PASS
    # The exposure: a four-minute-old census must not approve a BUY.
    assert decision.outcome is RiskOutcome.REJECT, decision.reason_codes
    assert "STALE_OR_FUTURE_SAFETY_DATA" in decision.reason_codes
    assert market.lp_control.created_at == now - timedelta(minutes=4)
    # Reported before SENTINEL is asked, as a refreshable stale source.
    assert too_old_for(market, now, RiskLimits()) == "LP_CONTROL_OLDER_THAN_RISK_LIMIT"


async def test_a_census_inside_the_risk_limit_still_passes(now, intent, context) -> None:
    payload, pool_id = await launch(now, census_age=timedelta(seconds=5))
    market, decision = sentinel(now, intent, context, payload, pool_id=pool_id)
    assert market.lp_control is not None
    assert market.lp_control.status is LpControlStatus.PASS
    assert market.lp_control.created_at == now - timedelta(seconds=5)
    assert too_old_for(market, now, RiskLimits()) is None
    assert decision.outcome is RiskOutcome.APPROVE, decision.reason_codes


async def test_a_census_without_its_block_time_is_unknown(now, intent, context) -> None:
    # Evidence recorded before the census carried its block time.
    payload, pool_id = await launch(now, census_age=timedelta(seconds=5))
    assert payload.intelligence is not None and payload.intelligence.pool_control is not None
    control = payload.intelligence.pool_control.model_copy(
        update={"snapshot_block_timestamp": None}
    )
    legacy = payload.model_copy(
        update={"intelligence": payload.intelligence.model_copy(update={"pool_control": control})}
    )
    market, decision = sentinel(now, intent, context, legacy, pool_id=pool_id)
    assert market.lp_control is not None
    assert (market.lp_control.status, market.lp_control.detail) == (
        LpControlStatus.UNKNOWN,
        "V4_CENSUS_BLOCK_TIME_UNKNOWN",
    )
    assert decision.outcome is RiskOutcome.REJECT
