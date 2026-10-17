"""The V2/V3 LP-control decision: pure, deterministic, fail-closed (issue #102).

The same rules as the V4 census after issue #97, in this order:

1. no registered deployment for the market's chain, or for the evidence's
   factory (and position manager) -> UNSUPPORTED;
2. absent evidence, a census gap, evidence of another chain/pool/token, an unverified
   factory binding, an incomplete scan, an unreconciled holder or position set,
   an empty pool, or an unknown census block time -> UNKNOWN;
3. any liquidity the verified creator can withdraw now -> BLOCKED;
4. any liquidity whose custody is unresolved -> UNKNOWN;
5. any other withdrawable liquidity -> UNKNOWN (a third party cannot be told
   from the creator's other wallet);
6. only when every unit of LP supply, or every position's liquidity, is
   RESTRICTED -> PASS.

Nothing here reads a chain or a provider. Not wired into
`orchestration.riskrequest.service.lp_control_view`: a V2/V3 market stays
UNSUPPORTED there, and the risk engine accepts a PASS only on the V4 census
basis, so nothing decided here can let a BUY through today.
"""

from dataclasses import dataclass

from src.agents.atlas.lpcontrol.models import (
    V2LpEvidence,
    V3LpEvidence,
    V3PositionKind,
    Withdrawal,
)
from src.agents.atlas.lpcontrol.registry import LpDeploymentRegistry
from src.core.models import LpControlStatus

LP_CONTROL_V2_LP_TOKEN_CENSUS = "V2_LP_TOKEN_CENSUS"
LP_CONTROL_V3_POSITION_CENSUS = "V3_POSITION_CENSUS"


@dataclass(frozen=True)
class MarketPool:
    """The market whose BUY is judged: its chain, 20-byte pool and base token."""

    chain: str
    pool: str
    token: str


@dataclass(frozen=True)
class LpControlDecision:
    status: LpControlStatus
    detail: str


def _unknown(detail: str) -> LpControlDecision:
    return LpControlDecision(LpControlStatus.UNKNOWN, detail)


def _withdrawal_verdict(
    prefix: str, held: list[tuple[Withdrawal, bool | None]], passed: str
) -> LpControlDecision:
    """Steps 3-6 over every (withdrawal, controller_is_creator) of nonzero liquidity."""
    if any(kind is Withdrawal.WITHDRAWABLE and creator is True for kind, creator in held):
        return LpControlDecision(LpControlStatus.BLOCKED, f"{prefix}_CREATOR_CAN_WITHDRAW")
    if any(kind is Withdrawal.UNRESOLVED for kind, _ in held):
        return _unknown(f"{prefix}_LP_CUSTODY_UNRESOLVED")
    if any(kind is Withdrawal.WITHDRAWABLE for kind, _ in held):
        return _unknown(f"{prefix}_LIQUIDITY_WITHDRAWABLE")
    if not held or any(kind is not Withdrawal.RESTRICTED for kind, _ in held):
        return _unknown(f"{prefix}_LP_CUSTODY_UNRESOLVED")  # pragma: no cover - closed enum
    return LpControlDecision(LpControlStatus.PASS, passed)


def decide_v2(
    evidence: V2LpEvidence | None, *, market: MarketPool, registry: LpDeploymentRegistry
) -> LpControlDecision:
    if not registry.v2_on(market.chain):
        return LpControlDecision(LpControlStatus.UNSUPPORTED, "V2_DEPLOYMENT_UNREGISTERED")
    if evidence is None:
        return _unknown("V2_LP_EVIDENCE_ABSENT")
    if evidence.chain != market.chain:
        return _unknown("V2_EVIDENCE_NOT_BOUND_TO_MARKET")
    binding = evidence.binding
    deployment = registry.v2_factory(evidence.chain, evidence.chain_id, binding.factory)
    if deployment is None:
        return LpControlDecision(LpControlStatus.UNSUPPORTED, "V2_FACTORY_UNREGISTERED")
    if evidence.gap is not None:
        return _unknown(evidence.gap)
    if binding.pair != market.pool or market.token not in (binding.token0, binding.token1):
        return _unknown("V2_EVIDENCE_NOT_BOUND_TO_MARKET")
    if not (
        binding.factory_get_pair == binding.pair
        and binding.pair_factory == binding.factory
        and binding.code_matches
        and binding.pair_code_hash == deployment.pair_code_hash
    ):
        return _unknown("V2_FACTORY_BINDING_UNVERIFIED")
    if not evidence.scan_complete:
        return _unknown("V2_HOLDER_SET_INCOMPLETE")
    if not evidence.reconciled:
        return _unknown("V2_HOLDER_SET_UNRECONCILED")
    if not evidence.total_supply_raw:
        return _unknown("V2_LP_SUPPLY_EMPTY")
    if evidence.block_timestamp is None:
        return _unknown("V2_CENSUS_BLOCK_TIME_UNKNOWN")
    held = [
        (item.custody.withdrawal, item.custody.controller_is_creator)
        for item in evidence.holders
        if item.balance_raw > 0
    ]
    return _withdrawal_verdict("V2", held, LP_CONTROL_V2_LP_TOKEN_CENSUS)


def decide_v3(
    evidence: V3LpEvidence | None, *, market: MarketPool, registry: LpDeploymentRegistry
) -> LpControlDecision:
    if not registry.v3_on(market.chain):
        return LpControlDecision(LpControlStatus.UNSUPPORTED, "V3_DEPLOYMENT_UNREGISTERED")
    if evidence is None:
        return _unknown("V3_LP_EVIDENCE_ABSENT")
    if evidence.chain != market.chain:
        return _unknown("V3_EVIDENCE_NOT_BOUND_TO_MARKET")
    binding = evidence.binding
    deployment = registry.v3_factory(
        evidence.chain, evidence.chain_id, binding.factory, binding.position_manager
    )
    if deployment is None:
        return LpControlDecision(LpControlStatus.UNSUPPORTED, "V3_FACTORY_UNREGISTERED")
    if evidence.gap is not None:
        return _unknown(evidence.gap)
    if (
        binding.pool != market.pool
        or market.token not in (binding.token0, binding.token1)
        or any(
            item.owner != binding.position_manager
            for item in evidence.positions
            if item.kind == V3PositionKind.POSITION_MANAGER
        )
    ):
        return _unknown("V3_EVIDENCE_NOT_BOUND_TO_MARKET")
    if not (
        binding.factory_get_pool == binding.pool
        and binding.pool_factory == binding.factory
        and binding.pool_code_matches
        and binding.position_manager_factory == binding.factory
    ):
        return _unknown("V3_FACTORY_BINDING_UNVERIFIED")
    if not evidence.scan_complete:
        return _unknown("V3_POSITION_SET_INCOMPLETE")
    if not (
        evidence.position_manager_reconciled
        and evidence.active_liquidity_reconciled
        and evidence.active_liquidity is not None
        and evidence.active_sum() == evidence.active_liquidity
    ):
        return _unknown("V3_POSITION_SET_UNRECONCILED")
    if not any(item.liquidity for item in evidence.positions):
        return _unknown("V3_LIQUIDITY_EMPTY")
    if evidence.block_timestamp is None:
        return _unknown("V3_CENSUS_BLOCK_TIME_UNKNOWN")
    held = [
        (
            item.withdrawal,
            None if item.custody is None else item.custody.controller_is_creator,
        )
        for item in evidence.positions
        if item.liquidity > 0
    ]
    return _withdrawal_verdict("V3", held, LP_CONTROL_V3_POSITION_CENSUS)
