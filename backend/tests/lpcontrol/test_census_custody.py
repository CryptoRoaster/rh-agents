"""Issue #92, final review finding 1: a V4 PASS needs every position's control.

The economic concentration tolerates up to 1 % of supply without a traced owner,
because it is a holder figure: accrued fees and round-down always leave some.
LP control is a different question -- who can withdraw the pool's liquidity --
and a token-supply tolerance proves nothing about it. A position with an
unknown owner may hold almost no token and most of the pool's ETH. So a PASS
requires no unattributed position, no unattributed PoolManager balance, an
exact concentration and every position's control resolved; anything else is
UNKNOWN. Every scenario runs through the real ATLAS census and payload.
"""

from src.agents.atlas.handler import onchain_payload as atlas_payload
from src.agents.atlas.models import HolderCompleteness, HolderSourceRow
from src.agents.atlas.policy import evaluate_snapshot
from src.agents.atlas.v4.math import position_amounts
from src.core.models import LpControlStatus, RiskOutcome
from src.orchestration.riskrequest.service import lp_control_view
from src.orchestration.workflow.models import OnchainPayload
from tests.atlas.v4.chain import POOL_MANAGER, FakeV4Chain, Pool
from tests.atlas.v4.scenarios import UNIT, build, official_launch
from tests.lpcontrol.test_lp_control import recorded, sentinel

# A contract that holds a position directly in the PoolManager. Its code was
# never verified, so who it acts for is unknown.
VAULT = "0x" + "7a" * 20
SALT = "0x" + "00" * 32


def unknown_position(chain: FakeV4Chain, pool: Pool, lower: int, liquidity: int) -> int:
    """A direct position held by an unverified contract, up to far above the price.

    Returns the token units it holds. Its range above the current tick is all
    ETH (currency0): the part of the pool a rug would take.
    """
    chain.contracts.add(VAULT)
    upper = pool.tick + 200_000
    upper -= upper % pool.tick_spacing
    chain.modify(pool, VAULT, lower, upper, liquidity, SALT)
    eth, token = position_amounts(pool.sqrt_price, lower, upper, liquidity)
    assert eth > 0
    return token


async def launch(now, *, extra: int = 0, unknown: str | None = None, reconcilable: bool = True):
    """An official launch with buyers; ``extra`` units the PoolManager keeps beyond
    every position, and optionally an unknown-owner position in the traded pool.
    ``reconcilable=False`` makes a liability unreadable (issue #96), so the census
    cannot account for what the PoolManager holds beyond the positions.
    """
    chain, pool, rows = official_launch()
    chain.extra_pool_balance = extra
    if not reconcilable:
        chain.protocol_fees = None
    if unknown == "token-and-eth":
        # One tick spacing below the price: a sliver of token, the rest ETH.
        token = unknown_position(chain, pool, pool.tick - pool.tick_spacing, 10**23)
        assert 0 < token * 100 <= chain.pool_balance(chain.head)
    elif unknown == "eth-only":
        # Entirely above the price: no token at all, only ETH.
        lower = pool.tick + 1000
        token = unknown_position(chain, pool, lower - lower % pool.tick_spacing, 10**30)
        assert token == 0
    buyers = rows[1:]
    rows = (
        HolderSourceRow(address=POOL_MANAGER, balance_raw=chain.pool_balance(chain.head)),
        *buyers,
    )
    snapshot = await build(
        now,
        chain,
        rows=rows,
        completeness=HolderCompleteness.COMPLETE,
        supply=sum(row.balance_raw for row in rows),
        holder_count=len(rows),
    )
    return atlas_payload(snapshot, evaluate_snapshot(snapshot, now)), pool.id


def census(payload: OnchainPayload):
    assert payload.intelligence is not None
    assert payload.intelligence.pool_control is not None
    return payload.intelligence.pool_control


async def test_an_unknown_owner_with_little_token_and_much_eth_is_unknown(
    now, intent, context
) -> None:
    # A: within the 1 % token tolerance, so the economic figure is available --
    # but nobody knows who can withdraw the position's ETH.
    payload, pool_id = await launch(now, unknown="token-and-eth")
    control = census(payload)
    assert control.economic_concentration is not None
    assert control.positions_unattributed == 1
    market, decision = sentinel(now, intent, context, payload, pool_id=pool_id)
    assert market.lp_control is not None
    assert market.lp_control.status is LpControlStatus.UNKNOWN
    assert market.lp_control.detail == "V4_POSITION_OWNER_UNKNOWN"
    assert decision.outcome is RiskOutcome.REJECT
    assert "LP_CONTROL_UNKNOWN" in decision.reason_codes


async def test_an_unattributed_pool_manager_balance_is_unknown(now, intent, context) -> None:
    # B: every position resolved, but the PoolManager holds token no position
    # accounts for -- inside the old 1 % tolerance, and still not evidence.
    # Since issue #96 a remainder passes only when the census reconciled it
    # against every liability (tests/lpcontrol/test_fee_reconciliation.py);
    # here it could not, so the remainder is as unexplained as it always was.
    payload, pool_id = await launch(now, extra=3 * UNIT, reconcilable=False)
    control = census(payload)
    assert control.economic_concentration is not None
    assert control.positions_unattributed == 0
    assert control.unattributed_raw == str(3 * UNIT)
    market, decision = sentinel(now, intent, context, payload, pool_id=pool_id)
    assert market.lp_control is not None
    assert market.lp_control.status is LpControlStatus.UNKNOWN
    assert market.lp_control.detail == "V4_POOL_BALANCE_UNATTRIBUTED"
    assert decision.outcome is RiskOutcome.REJECT
    assert "LP_CONTROL_UNKNOWN" in decision.reason_codes


async def test_an_exact_concentration_with_an_unresolved_position_is_unknown(
    now, intent, context
) -> None:
    # C: the position holds no token, so the economic figure is complete and
    # exact -- yet the LP control of its ETH is unresolved.
    payload, pool_id = await launch(now, unknown="eth-only")
    control = census(payload)
    assert control.economic_concentration is not None
    assert control.basis == "EXACT"
    assert control.unattributed_raw == "0"
    assert control.positions_unattributed == 1
    finding = lp_control_view(snapshot=recorded(now, pool_id=pool_id), onchain=payload)
    assert finding[:2] == (LpControlStatus.UNKNOWN, "V4_POSITION_OWNER_UNKNOWN")
    _, decision = sentinel(now, intent, context, payload, pool_id=pool_id)
    assert decision.outcome is RiskOutcome.REJECT
    assert "LP_CONTROL_UNKNOWN" in decision.reason_codes


async def test_a_complete_verified_census_without_unknowns_passes(now, intent, context) -> None:
    # D: every position attributed and resolved, nothing unattributed, exact.
    payload, pool_id = await launch(now)
    control = census(payload)
    assert control.positions_unattributed == 0
    assert control.unattributed_raw == "0"
    assert control.basis == "EXACT"
    market, decision = sentinel(now, intent, context, payload, pool_id=pool_id)
    assert market.lp_control is not None
    assert market.lp_control.status is LpControlStatus.PASS
    assert decision.outcome is RiskOutcome.APPROVE, decision.reason_codes


def with_census(payload: OnchainPayload, **changes: object) -> OnchainPayload:
    assert payload.intelligence is not None
    control = census(payload).model_copy(update=changes)
    intelligence = payload.intelligence.model_copy(update={"pool_control": control})
    return payload.model_copy(update={"intelligence": intelligence})


async def test_every_other_gap_in_position_control_is_unknown(now) -> None:
    payload, pool_id = await launch(now)
    snapshot = recorded(now, pool_id=pool_id)
    assert lp_control_view(snapshot=snapshot, onchain=payload)[0] is LpControlStatus.PASS
    cases = {
        "V4_CONCENTRATION_NOT_EXACT": {"basis": "UPPER_BOUND"},
        # Recorded before position control facts existed.
        "V4_POSITION_CONTROL_UNRESOLVED": {"position_control_states": None},
    }
    for state in ("TIMELOCKED", "UNKNOWN_CONTRACT_CUSTODY", "SOMETHING_NEW"):
        cases[f"state:{state}"] = {"position_control_states": {state: 1}}
    cases["empty_states"] = {"position_control_states": {}}
    cases["timelocked_raw"] = {"timelocked_raw": "1"}
    cases["unknown_custody_raw"] = {"unknown_custody_raw": "1"}
    cases["unattributed_raw_absent"] = {"unattributed_raw": None}
    for label, changes in cases.items():
        status, detail = lp_control_view(
            snapshot=snapshot, onchain=with_census(payload, **changes)
        )[:2]
        assert status is LpControlStatus.UNKNOWN, label
        if label.startswith("V4_"):
            assert detail == label
