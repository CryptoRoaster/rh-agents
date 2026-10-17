"""Issue #102: the V2/V3 LP-control decision, on FIXTURE evidence only.

The rules are the V4 rules after issue #97: a purchase passes only when every
unit of LP supply (V2) or every position's liquidity (V3) in the market's own
pool is permanently restricted -- burned or held by a verified permanent lock.
Liquidity the verified creator can withdraw is BLOCKED; any other withdrawable
liquidity is UNKNOWN, since nobody here can tell a third party from the
creator's other wallet; any gap is UNKNOWN; and a venue without a registry
entry is UNSUPPORTED. The production registry is empty, so in production every
one of these cases is UNSUPPORTED (`test_v2v3_registry.py`).
"""

import pytest

from src.agents.atlas.lpcontrol.custody import BURN_DEAD, ZERO_ADDRESS
from src.agents.atlas.lpcontrol.decision import (
    LP_CONTROL_V2_LP_TOKEN_CENSUS,
    LP_CONTROL_V3_POSITION_CENSUS,
    decide_v2,
    decide_v3,
)
from src.agents.atlas.lpcontrol.models import (
    CustodyFacts,
    HolderState,
    V3PositionEvidence,
    Withdrawal,
)
from src.agents.atlas.lpcontrol.registry import LP_DEPLOYMENTS, LpDeploymentRegistry
from src.core.models import LpControlStatus
from tests.lpcontrol.v2v3_fixtures import (
    CREATOR,
    FIXTURE_CHAIN_ID,
    FIXTURE_REGISTRY,
    LOCKER,
    NPM,
    QUOTE,
    THIRD_PARTY,
    V2_MARKET,
    V3_MARKET,
    account,
    burned,
    direct_position,
    locked,
    npm_position,
    pool_redeemable,
    releasable,
    timelocked,
    unknown_contract,
    v2_binding,
    v2_evidence,
    v3_binding,
    v3_evidence,
)


def v2(evidence, registry=FIXTURE_REGISTRY, market=V2_MARKET):
    found = decide_v2(evidence, market=market, registry=registry)
    return found.status, found.detail


def v3(evidence, registry=FIXTURE_REGISTRY, market=V3_MARKET):
    found = decide_v3(evidence, market=market, registry=registry)
    return found.status, found.detail


# ------------------------------------------------------------- classification


@pytest.mark.parametrize(
    ("facts", "withdrawal"),
    [
        (burned(ZERO_ADDRESS), Withdrawal.RESTRICTED),
        (burned(BURN_DEAD), Withdrawal.RESTRICTED),
        (locked(), Withdrawal.RESTRICTED),
        (account(THIRD_PARTY), Withdrawal.WITHDRAWABLE),
        (releasable(THIRD_PARTY), Withdrawal.WITHDRAWABLE),
        (pool_redeemable(), Withdrawal.WITHDRAWABLE),
        (timelocked(), Withdrawal.UNRESOLVED),
        (unknown_contract(), Withdrawal.UNRESOLVED),
    ],
)
def test_every_holder_state_has_one_withdrawal_class(facts, withdrawal) -> None:
    assert facts.withdrawal is withdrawal


def test_only_a_burn_address_is_burned() -> None:
    with pytest.raises(ValueError, match="burn address"):
        burned(THIRD_PARTY)


def test_a_lock_rests_on_verified_locker_code() -> None:
    with pytest.raises(ValueError, match="verified locker code"):
        CustodyFacts.model_validate({**locked().model_dump(), "proof_contract": None})


def test_every_holder_state_is_classified() -> None:
    assert {facts.state for facts in (
        burned(), locked(), account(THIRD_PARTY), releasable(THIRD_PARTY),
        pool_redeemable(), timelocked(), unknown_contract(),
    )} == set(HolderState)  # fmt: skip


# -------------------------------------------------------------------- V2 PASS


def test_v2_all_supply_burned_passes() -> None:
    assert v2(v2_evidence()) == (LpControlStatus.PASS, LP_CONTROL_V2_LP_TOKEN_CENSUS)


def test_v2_minimum_liquidity_burned_and_the_rest_locked_passes() -> None:
    evidence = v2_evidence(((burned(ZERO_ADDRESS), 1_000), (locked(), 10_000_000)))
    assert v2(evidence) == (LpControlStatus.PASS, LP_CONTROL_V2_LP_TOKEN_CENSUS)


def test_v2_a_zero_balance_holder_does_not_count() -> None:
    evidence = v2_evidence(
        ((burned(ZERO_ADDRESS), 1_000), (burned(BURN_DEAD), 9_000), (account(CREATOR), 0))
    )
    assert v2(evidence)[0] is LpControlStatus.PASS


# ----------------------------------------------------------- V2 withdrawable


def test_v2_creator_held_lp_tokens_are_blocked() -> None:
    evidence = v2_evidence(((burned(ZERO_ADDRESS), 1_000), (account(CREATOR), 999_000)))
    assert v2(evidence) == (LpControlStatus.BLOCKED, "V2_CREATOR_CAN_WITHDRAW")


def test_v2_a_released_lock_the_creator_operates_is_blocked() -> None:
    evidence = v2_evidence(((burned(), 1_000), (releasable(CREATOR), 999_000)))
    assert v2(evidence) == (LpControlStatus.BLOCKED, "V2_CREATOR_CAN_WITHDRAW")


def test_v2_creator_withdrawable_blocks_even_beside_unresolved_custody() -> None:
    evidence = v2_evidence(((burned(), 1_000), (unknown_contract(), 5_000), (account(CREATOR), 1)))
    assert v2(evidence) == (LpControlStatus.BLOCKED, "V2_CREATOR_CAN_WITHDRAW")


@pytest.mark.parametrize(
    "holder",
    [
        account(THIRD_PARTY),
        # The creator is unknown: still withdrawable, never PASS.
        CustodyFacts.model_validate(
            {**account(THIRD_PARTY).model_dump(), "controller_is_creator": None}
        ),
        releasable(THIRD_PARTY, holder="0x" + "11" * 20),
    ],
)
def test_v2_third_party_withdrawable_is_unknown(holder) -> None:
    evidence = v2_evidence(((burned(), 1_000), (locked(), 900_000), (holder, 1)))
    assert v2(evidence) == (LpControlStatus.UNKNOWN, "V2_LIQUIDITY_WITHDRAWABLE")


def test_v2_lp_tokens_left_in_the_pair_are_withdrawable_by_anyone() -> None:
    evidence = v2_evidence(((burned(), 1_000), (pool_redeemable(), 5)))
    assert v2(evidence) == (LpControlStatus.UNKNOWN, "V2_LIQUIDITY_WITHDRAWABLE")


@pytest.mark.parametrize("holder", [timelocked(), unknown_contract()])
def test_v2_unresolved_custody_is_unknown(holder) -> None:
    evidence = v2_evidence(((burned(), 1_000), (holder, 999_000)))
    assert v2(evidence) == (LpControlStatus.UNKNOWN, "V2_LP_CUSTODY_UNRESOLVED")


# ------------------------------------------------------------- V2 incomplete


def test_v2_absent_evidence_with_a_registered_venue_is_unknown() -> None:
    assert v2(None) == (LpControlStatus.UNKNOWN, "V2_LP_EVIDENCE_ABSENT")


@pytest.mark.parametrize(
    ("changes", "detail"),
    [
        ({"scan_complete": False}, "V2_HOLDER_SET_INCOMPLETE"),
        ({"gap": "V2_READ_BUDGET_EXHAUSTED"}, "V2_READ_BUDGET_EXHAUSTED"),
        ({"total_supply_raw": 1_000_001}, "V2_HOLDER_SET_UNRECONCILED"),
        ({"total_supply_raw": 999_999}, "V2_HOLDER_SET_UNRECONCILED"),
        ({"total_supply_raw": None}, "V2_HOLDER_SET_UNRECONCILED"),
        ({"holders": (), "total_supply_raw": 0}, "V2_LP_SUPPLY_EMPTY"),
        ({"block_timestamp": None}, "V2_CENSUS_BLOCK_TIME_UNKNOWN"),
        ({"chain": "other-chain"}, "V2_EVIDENCE_NOT_BOUND_TO_MARKET"),
        ({"binding": v2_binding(token0=QUOTE, token1="0x" + "99" * 20)},
         "V2_EVIDENCE_NOT_BOUND_TO_MARKET"),
        ({"binding": v2_binding(factory_get_pair="0x" + "00" * 20)},
         "V2_FACTORY_BINDING_UNVERIFIED"),
        ({"binding": v2_binding(factory_get_pair=None)}, "V2_FACTORY_BINDING_UNVERIFIED"),
        ({"binding": v2_binding(pair_factory=THIRD_PARTY)}, "V2_FACTORY_BINDING_UNVERIFIED"),
        ({"binding": v2_binding(code_matches=False)}, "V2_FACTORY_BINDING_UNVERIFIED"),
        ({"binding": v2_binding(pair_code_hash="0x" + "dd" * 32)},
         "V2_FACTORY_BINDING_UNVERIFIED"),
    ],
)  # fmt: skip
def test_v2_every_gap_is_unknown(changes, detail) -> None:
    assert v2(v2_evidence(**changes)) == (LpControlStatus.UNKNOWN, detail)


def test_v2_a_gap_is_unknown_even_with_creator_lp_in_hand() -> None:
    # The holder set did not reconcile, so no balance in it is a fact.
    evidence = v2_evidence(((account(CREATOR), 5),), total_supply_raw=6)
    assert v2(evidence) == (LpControlStatus.UNKNOWN, "V2_HOLDER_SET_UNRECONCILED")


def test_v2_duplicate_holders_are_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="once"):
        v2_evidence(((burned(), 1), (burned(), 2)))


# ------------------------------------------------------------ V2 unsupported


def test_v2_an_empty_registry_is_unsupported_whatever_the_evidence() -> None:
    for evidence in (None, v2_evidence(), v2_evidence(scan_complete=False)):
        assert v2(evidence, registry=LpDeploymentRegistry()) == (
            LpControlStatus.UNSUPPORTED,
            "V2_DEPLOYMENT_UNREGISTERED",
        )


def test_v2_the_production_registry_is_unsupported_for_perfect_evidence() -> None:
    assert v2(v2_evidence(), registry=LP_DEPLOYMENTS)[0] is LpControlStatus.UNSUPPORTED


def test_v2_an_unregistered_factory_is_unsupported() -> None:
    evidence = v2_evidence(binding=v2_binding(factory=THIRD_PARTY, pair_factory=THIRD_PARTY))
    assert v2(evidence) == (LpControlStatus.UNSUPPORTED, "V2_FACTORY_UNREGISTERED")


def test_v2_a_registered_factory_on_another_chain_id_is_unsupported() -> None:
    evidence = v2_evidence(chain_id=FIXTURE_CHAIN_ID + 1)
    assert v2(evidence) == (LpControlStatus.UNSUPPORTED, "V2_FACTORY_UNREGISTERED")


# -------------------------------------------------------------------- V3 PASS


def test_v3_every_position_nft_locked_passes() -> None:
    assert v3(v3_evidence()) == (LpControlStatus.PASS, LP_CONTROL_V3_POSITION_CENSUS)


def test_v3_an_nft_held_by_a_burn_address_is_restricted() -> None:
    positions = (npm_position(1, 10, locked()), npm_position(2, 5, burned(BURN_DEAD)))
    assert v3(v3_evidence(positions))[0] is LpControlStatus.PASS


def test_v3_an_emptied_position_does_not_count() -> None:
    positions = (npm_position(1, 10, locked()), npm_position(2, 0, account(CREATOR)))
    assert v3(v3_evidence(positions, active_liquidity=10))[0] is LpControlStatus.PASS


# ------------------------------------------------------------ V3 withdrawable


def test_v3_a_creator_owned_position_nft_is_blocked() -> None:
    positions = (npm_position(1, 10, locked()), npm_position(2, 5, account(CREATOR)))
    assert v3(v3_evidence(positions)) == (LpControlStatus.BLOCKED, "V3_CREATOR_CAN_WITHDRAW")


def test_v3_a_creator_direct_pool_position_is_blocked() -> None:
    positions = (npm_position(1, 10, locked()), direct_position(CREATOR, 5, account(CREATOR)))
    assert v3(v3_evidence(positions)) == (LpControlStatus.BLOCKED, "V3_CREATOR_CAN_WITHDRAW")


def test_v3_a_third_party_position_is_unknown() -> None:
    positions = (npm_position(1, 10, locked()), npm_position(2, 5, account(THIRD_PARTY)))
    assert v3(v3_evidence(positions)) == (LpControlStatus.UNKNOWN, "V3_LIQUIDITY_WITHDRAWABLE")


@pytest.mark.parametrize("custody", [None, timelocked(), unknown_contract()])
def test_v3_unresolved_custody_is_unknown(custody) -> None:
    positions = (npm_position(1, 10, locked()), npm_position(2, 5, custody))
    assert v3(v3_evidence(positions)) == (LpControlStatus.UNKNOWN, "V3_LP_CUSTODY_UNRESOLVED")


def test_v3_a_direct_position_of_an_unknown_contract_is_unknown() -> None:
    positions = (direct_position(LOCKER, 5, unknown_contract(LOCKER)),)
    assert v3(v3_evidence(positions)) == (LpControlStatus.UNKNOWN, "V3_LP_CUSTODY_UNRESOLVED")


# ------------------------------------------------------------- V3 incomplete


@pytest.mark.parametrize(
    ("changes", "detail"),
    [
        ({"scan_complete": False}, "V3_POSITION_SET_INCOMPLETE"),
        ({"gap": "V3_READ_FAILED"}, "V3_READ_FAILED"),
        ({"position_manager_reconciled": False}, "V3_POSITION_SET_UNRECONCILED"),
        ({"active_liquidity_reconciled": False}, "V3_POSITION_SET_UNRECONCILED"),
        ({"positions": (), "active_liquidity": 0}, "V3_LIQUIDITY_EMPTY"),
        ({"block_timestamp": None}, "V3_CENSUS_BLOCK_TIME_UNKNOWN"),
        ({"binding": v3_binding(pool="0x" + "99" * 20, factory_get_pool="0x" + "99" * 20)},
         "V3_EVIDENCE_NOT_BOUND_TO_MARKET"),
        ({"binding": v3_binding(factory_get_pool=None)}, "V3_FACTORY_BINDING_UNVERIFIED"),
        ({"binding": v3_binding(pool_factory=THIRD_PARTY)}, "V3_FACTORY_BINDING_UNVERIFIED"),
        ({"binding": v3_binding(pool_code_matches=False)}, "V3_FACTORY_BINDING_UNVERIFIED"),
        ({"binding": v3_binding(position_manager_factory=None)},
         "V3_FACTORY_BINDING_UNVERIFIED"),
    ],
)  # fmt: skip
def test_v3_every_gap_is_unknown(changes, detail) -> None:
    assert v3(v3_evidence(**changes)) == (LpControlStatus.UNKNOWN, detail)


def test_v3_a_position_manager_position_without_its_token_id_is_refused() -> None:
    with pytest.raises(ValueError, match="token id"):
        V3PositionEvidence.model_validate(
            {**npm_position(1, 1, locked()).model_dump(), "token_id": None}
        )


def test_v3_absent_evidence_with_a_registered_venue_is_unknown() -> None:
    assert v3(None) == (LpControlStatus.UNKNOWN, "V3_LP_EVIDENCE_ABSENT")


# ------------------------------------------------------------ V3 unsupported


def test_v3_an_empty_registry_is_unsupported_whatever_the_evidence() -> None:
    for evidence in (None, v3_evidence(), v3_evidence(scan_complete=False)):
        assert v3(evidence, registry=LpDeploymentRegistry()) == (
            LpControlStatus.UNSUPPORTED,
            "V3_DEPLOYMENT_UNREGISTERED",
        )
    assert v3(v3_evidence(), registry=LP_DEPLOYMENTS)[0] is LpControlStatus.UNSUPPORTED


def test_v3_an_unregistered_position_manager_is_unsupported() -> None:
    evidence = v3_evidence(binding=v3_binding(position_manager=THIRD_PARTY))
    assert v3(evidence) == (LpControlStatus.UNSUPPORTED, "V3_FACTORY_UNREGISTERED")


def test_v3_a_position_of_another_position_manager_is_refused() -> None:
    other = npm_position(2, 5, locked()).model_copy(update={"owner": THIRD_PARTY})
    evidence = v3_evidence((npm_position(1, 10, locked()), other))
    assert v3(evidence) == (LpControlStatus.UNKNOWN, "V3_EVIDENCE_NOT_BOUND_TO_MARKET")
    assert NPM != THIRD_PARTY
