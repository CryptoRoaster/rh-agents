"""Issue #97: known control is not restricted withdrawal.

A PASS used to mean only that every position's controller was established. A
creator holding the launch NFT in an account is an established controller --
and can remove the market's liquidity in the next block. The strategic decision
for Phase 0 (2026-10-17): a purchase passes only when nobody can withdraw the
market pool's liquidity now. In the market pool:

* every position permanently locked -> PASS;
* any position the verified creator can withdraw -> BLOCKED;
* any other withdrawable position (an account's NFT, a released timelock, a
  direct position of an account) -> UNKNOWN, because nobody here can tell a
  third party from the creator's other wallet.

Positions in the token's other pools are still judged for custody as before;
removing them does not drain the market's pool. Every scenario runs through
the real ATLAS census and payload.
"""

from src.agents.atlas.handler import onchain_payload as atlas_payload
from src.agents.atlas.models import HolderCompleteness, HolderSourceRow, OriginVerification
from src.agents.atlas.policy import evaluate_snapshot
from src.core.models import LpControlStatus, RiskOutcome
from src.orchestration.workflow.models import OnchainPayload
from tests.atlas.conftest import CREATOR, TOKEN
from tests.atlas.v4.chain import NATIVE, POOL_MANAGER, FakeV4Chain, Pool
from tests.atlas.v4.custody import OFFICIAL_SPLITTER, TIMELOCK_RECIPIENT, timelocked_code
from tests.atlas.v4.scenarios import (
    LAUNCH_TICK,
    build,
    official_launch,
    origin,
)
from tests.lpcontrol.test_lp_control import sentinel

THIRD_PARTY = "0x" + "3f" * 20


async def launch(now, *, owner: str = OFFICIAL_SPLITTER, configure=None, verified: bool = True):
    chain, pool, rows = official_launch(owner=owner)
    chain.extra_pool_balance = 0
    if configure is not None:
        configure(chain, pool)
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
        origin_facts=origin(
            verification=(
                OriginVerification.RECEIPT_CONFIRMED if verified else OriginVerification.UNVERIFIED
            )
        ),
    )
    return atlas_payload(snapshot, evaluate_snapshot(snapshot, now)), pool.id


def judged(now, intent, context, payload: OnchainPayload, pool_id: str):
    market, decision = sentinel(now, intent, context, payload, pool_id=pool_id)
    assert market.lp_control is not None
    return market.lp_control, decision


async def test_a_creator_holding_the_launch_nft_is_blocked(now, intent, context) -> None:
    payload, pool_id = await launch(now, owner=CREATOR)
    lp, decision = judged(now, intent, context, payload, pool_id)
    assert (lp.status, lp.detail) == (LpControlStatus.BLOCKED, "V4_CREATOR_CAN_WITHDRAW")
    assert decision.outcome is RiskOutcome.REJECT
    assert "LP_CONTROL_BLOCKED" in decision.reason_codes


async def test_a_timelock_released_to_the_creator_is_blocked(now, intent, context) -> None:
    def configure(chain: FakeV4Chain, pool: Pool) -> None:
        chain.codes[TIMELOCK_RECIPIENT] = timelocked_code(timelock=1, operator=CREATOR)

    payload, pool_id = await launch(now, owner=TIMELOCK_RECIPIENT, configure=configure)
    lp, decision = judged(now, intent, context, payload, pool_id)
    assert (lp.status, lp.detail) == (LpControlStatus.BLOCKED, "V4_CREATOR_CAN_WITHDRAW")
    assert decision.outcome is RiskOutcome.REJECT


async def test_any_other_account_holding_the_launch_nft_is_unknown(now, intent, context) -> None:
    payload, pool_id = await launch(now, owner=THIRD_PARTY)
    lp, decision = judged(now, intent, context, payload, pool_id)
    assert (lp.status, lp.detail) == (LpControlStatus.UNKNOWN, "V4_LIQUIDITY_WITHDRAWABLE")
    assert decision.outcome is RiskOutcome.REJECT


async def test_a_creator_nobody_verified_cannot_be_told_apart(now, intent, context) -> None:
    # Without a receipt-confirmed creator the same NFT is withdrawable by
    # somebody unnamed: not BLOCKED, and certainly not PASS.
    payload, pool_id = await launch(now, owner=CREATOR, verified=False)
    lp, _ = judged(now, intent, context, payload, pool_id)
    assert (lp.status, lp.detail) == (LpControlStatus.UNKNOWN, "V4_LIQUIDITY_WITHDRAWABLE")


async def test_one_withdrawable_position_beside_a_locked_launch_is_unknown(
    now, intent, context
) -> None:
    def configure(chain: FakeV4Chain, pool: Pool) -> None:
        # A small account position above the price: only ETH, no token.
        lower = LAUNCH_TICK + 1_000
        lower -= lower % pool.tick_spacing
        chain.mint(pool, THIRD_PARTY, 77, lower, lower + 10 * pool.tick_spacing, 10**18)

    payload, pool_id = await launch(now, configure=configure)
    lp, _ = judged(now, intent, context, payload, pool_id)
    assert (lp.status, lp.detail) == (LpControlStatus.UNKNOWN, "V4_LIQUIDITY_WITHDRAWABLE")


async def test_a_direct_account_position_in_the_market_pool_is_unknown(
    now, intent, context
) -> None:
    def configure(chain: FakeV4Chain, pool: Pool) -> None:
        lower = LAUNCH_TICK + 1_000
        lower -= lower % pool.tick_spacing
        chain.modify(
            pool, THIRD_PARTY, lower, lower + 10 * pool.tick_spacing, 10**18, "0x" + "00" * 32
        )

    payload, pool_id = await launch(now, configure=configure)
    lp, _ = judged(now, intent, context, payload, pool_id)
    assert (lp.status, lp.detail) == (LpControlStatus.UNKNOWN, "V4_LIQUIDITY_WITHDRAWABLE")


async def test_an_account_position_in_another_pool_does_not_touch_the_market(
    now, intent, context
) -> None:
    def configure(chain: FakeV4Chain, pool: Pool) -> None:
        other = chain.add_pool(
            Pool(currency0=NATIVE, currency1=TOKEN, fee=10_000, tick_spacing=200, tick=pool.tick)
        )
        lower = pool.tick + 2_000
        lower -= lower % other.tick_spacing
        chain.mint(other, THIRD_PARTY, 78, lower, lower + 10 * other.tick_spacing, 10**18)

    payload, pool_id = await launch(now, configure=configure)
    lp, decision = judged(now, intent, context, payload, pool_id)
    assert lp.status is LpControlStatus.PASS, lp.detail
    assert decision.outcome is RiskOutcome.APPROVE, decision.reason_codes


async def test_a_locked_launch_alone_passes(now, intent, context) -> None:
    payload, pool_id = await launch(now)
    lp, decision = judged(now, intent, context, payload, pool_id)
    assert lp.status is LpControlStatus.PASS, lp.detail
    assert decision.outcome is RiskOutcome.APPROVE, decision.reason_codes
    assert payload.intelligence is not None and payload.intelligence.pool_control is not None
    (market,) = [p for p in payload.intelligence.pool_control.pools if p.pool_id == pool_id]
    assert (market.positions_total, market.positions_withdrawal_restricted) == (1, 1)
    assert (market.positions_withdrawable, market.positions_creator_withdrawable) == (0, 0)
