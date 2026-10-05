"""Economic holder concentration: raw holders plus the V4 positions they control.

The raw holder record is never modified. This derives a second distribution
beside it, in which supply sitting in the PoolManager is moved — only as far as
it can be traced exactly — to the owners of the positions that hold it:

* the PoolManager's own row, if the holder provider reported one, leaves the
  distribution, because every unit of it is re-assigned below or remains
  explicitly unattributed — counting it as well would count the same tokens
  twice;
* each attributed position's exact token amount is added to its controller --
  the account that owns it, or the controller a verified release names
  (``control.py``); never to a custody contract as if it were a holder;
* a position verified as permanently locked is nobody's: its amount is recorded
  as locked liquidity and ranked for no holder;
* a position whose control is unresolved -- unverified contract custody, or a
  timelock not yet passed -- makes the figure unknown; only a floor is kept;
* whatever the PoolManager holds beyond that is ``UNATTRIBUTED_POOL_BALANCE``.

A fee entitlement is not control. Who is paid a position's fees has no bearing
here: holder concentration is about who can move principal supply.

Wherever a quantity can only be bounded it is ranked at its bound, so the
result may overstate concentration but can never understate it, and is labelled
``UPPER_BOUND`` when it does. An unattributed remainder too large to be fees
and rounding makes the whole figure unknown instead of falsely precise.
"""

from decimal import Decimal, localcontext

from src.agents.atlas.models import (
    BURN_ADDRESSES,
    ContractFacts,
    HolderCompleteness,
    HolderFacts,
    OriginFacts,
    OriginVerification,
)
from src.agents.atlas.sources.normalize import RETAINED_HOLDERS, TOP_N
from src.agents.atlas.v4.control import UNRESOLVED_STATES, PositionControlState
from src.agents.atlas.v4.models import (
    UNATTRIBUTED_POOL_BALANCE,
    ConcentrationBasis,
    EconomicHolder,
    PoolControlFacts,
    PoolControlGap,
    PositionOwnerStatus,
    V4Census,
    V4PositionFacts,
)
from src.core.numbers import quantize
from src.markets.models import Availability

# How much of total supply may sit in the PoolManager without a traced owner
# before the economic figure is refused. Not a concentration limit: a data
# completeness bound. Accrued but uncollected swap fees and the protocol's
# round-down of every position amount always leave some remainder, and that
# remainder is still ranked as one pseudo-holder. Anything larger is supply the
# census could not explain — a pool it missed, a position it could not read —
# and a concentration derived around it would be guesswork.
MAX_UNATTRIBUTED_POOL_FRACTION = Decimal("0.01")


def _share(amount: int, denominator: int) -> Decimal:
    with localcontext() as context:
        context.prec = 78
        return min(Decimal(1), quantize(Decimal(amount) / Decimal(denominator)))


def verified_creator(origin: OriginFacts) -> str | None:
    """The creator, only when its creation receipt was confirmed on-chain."""
    if (
        origin.status != Availability.AVAILABLE
        or origin.verification != OriginVerification.RECEIPT_CONFIRMED
    ):
        return None
    return origin.creator_address


def _with_creator(census: V4Census, creator: str | None) -> V4Census:
    """Mark which hooks and positions the verified creator controls, if known."""

    def matches(owner: str | None) -> bool | None:
        if creator is None or owner is None:
            return None
        return owner == creator

    pools = tuple(
        pool
        if pool.hook_facts is None
        else pool.model_copy(
            update={
                "hook_facts": pool.hook_facts.model_copy(
                    update={"owner_is_creator": matches(pool.hook_facts.owner)}
                )
            }
        )
        for pool in census.pools
    )
    positions = tuple(
        item.model_copy(update={"owner_is_creator": matches(item.owner)})
        for item in census.positions
    )
    return census.model_copy(update={"pools": pools, "positions": positions})


def pool_control(
    census: V4Census,
    holders: HolderFacts,
    contract: ContractFacts,
    origin: OriginFacts,
    *,
    required: bool,
    market_pool: str | None = None,
) -> PoolControlFacts:
    creator = verified_creator(origin)
    census = _with_creator(census, creator)
    if census.status != Availability.AVAILABLE or census.pool_manager is None:
        return PoolControlFacts(
            status=Availability.UNAVAILABLE,
            gap=census.gap or PoolControlGap.CENSUS_UNAVAILABLE,
            required=required,
            census=census,
        )
    if market_pool is not None and market_pool not in {pool.pool_id for pool in census.pools}:
        # The pool this market trades in was not found, so the census cannot be
        # the whole picture — however small any unexplained remainder looks.
        return PoolControlFacts(
            status=Availability.UNAVAILABLE,
            gap=PoolControlGap.MARKET_POOL_NOT_FOUND,
            required=required,
            census=census,
        )
    supply = contract.total_supply_raw if contract.status == Availability.AVAILABLE else None
    if (
        supply is None
        or supply <= 0
        or holders.status != Availability.AVAILABLE
        or holders.total_supply_raw != supply
        or not holders.top_holders
    ):
        # The economic view is built on the raw one and the same denominator.
        return PoolControlFacts(
            status=Availability.UNAVAILABLE,
            gap=PoolControlGap.HOLDER_BASIS_UNAVAILABLE,
            required=required,
            census=census,
            total_supply_raw=supply,
        )

    held = census.pool_manager_balance_raw or 0
    attributed = [
        item for item in census.positions if item.owner_status == PositionOwnerStatus.ATTRIBUTED
    ]
    attributed_raw = sum(item.controlled_token_raw for item in attributed)
    owner_unknown_raw = sum(
        item.controlled_token_raw
        for item in census.positions
        if item.owner_status == PositionOwnerStatus.OWNER_UNKNOWN
    )
    buckets = _control_buckets(attributed)
    creator_raw = (
        None
        if creator is None
        else sum(item.controlled_token_raw for item in attributed if item.controller == creator)
    )
    figures = {
        "required": required,
        "census": census,
        "total_supply_raw": supply,
        "pool_held_supply_fraction": _share(held, supply),
        "creator_controlled_raw": creator_raw,
        "creator_controlled_pool_supply_fraction": (
            None if creator_raw is None else _share(creator_raw, supply)
        ),
    }
    if attributed_raw + owner_unknown_raw > held:
        # Positions cannot hold more than the PoolManager does. The census and
        # the balance describe different states, so neither can be trusted.
        return PoolControlFacts(
            status=Availability.UNAVAILABLE,
            gap=PoolControlGap.POOL_BALANCE_UNATTRIBUTED,
            **figures,  # type: ignore[arg-type]
        )
    unattributed_raw = held - attributed_raw
    figures |= {
        "attributed_raw": attributed_raw,
        "unattributed_raw": unattributed_raw,
        "attributable_pool_supply_fraction": _share(attributed_raw, supply),
        "unattributed_pool_supply_fraction": _share(unattributed_raw, supply),
    }
    for state, field in BUCKET_FIELDS.items():
        figures |= {
            f"{field}_raw": buckets[state],
            f"{field}_pool_supply_fraction": _share(buckets[state], supply),
        }
    if Decimal(unattributed_raw) > MAX_UNATTRIBUTED_POOL_FRACTION * supply:
        gap = (
            PoolControlGap.POSITION_OWNER_UNKNOWN
            if owner_unknown_raw > 0
            else PoolControlGap.POOL_BALANCE_UNATTRIBUTED
        )
        return PoolControlFacts(
            status=Availability.UNAVAILABLE,
            gap=gap,
            **figures,  # type: ignore[arg-type]
        )

    # Only the retained prefix of the raw distribution is known by address. An
    # owner outside it holds at most what the smallest retained row holds,
    # unless the holder set is complete and was retained whole.
    retained_whole = (
        holders.completeness == HolderCompleteness.COMPLETE
        and len(holders.top_holders) < RETAINED_HOLDERS
    )
    unseen = 0 if retained_whole else holders.top_holders[-1].balance_raw
    controlled = [
        (item.controller, item.controlled_token_raw)
        for item in attributed
        if item.controller is not None
    ]
    if any(buckets[state] for state in UNRESOLVED_STATES):
        # Supply nobody can be named for and nobody may call locked. Whatever
        # figure is put on it is a guess, so the concentration is unknown. What
        # is still established is a floor: the distribution without that
        # supply, with every owner credited only with what was seen.
        floor = _ranked(holders, census, controlled, unseen=0, unattributed_raw=0)
        return PoolControlFacts(
            status=Availability.UNAVAILABLE,
            gap=PoolControlGap.POSITION_CONTROL_UNRESOLVED,
            economic_top10_floor=_share(sum(value for _, value in floor[:TOP_N]), supply),
            **figures,  # type: ignore[arg-type]
        )

    ranked = _ranked(holders, census, controlled, unseen=unseen, unattributed_raw=unattributed_raw)
    basis = (
        ConcentrationBasis.UPPER_BOUND
        if unattributed_raw
        or (unseen and any(owner not in _retained(holders, census) for owner, _ in controlled))
        else ConcentrationBasis.EXACT
    )
    if not ranked or ranked[0][1] == 0:
        return PoolControlFacts(
            status=Availability.UNAVAILABLE,
            gap=PoolControlGap.HOLDER_BASIS_UNAVAILABLE,
            **figures,  # type: ignore[arg-type]
        )
    return PoolControlFacts(
        status=Availability.AVAILABLE,
        basis=basis,
        economic_top1_share=_share(ranked[0][1], supply),
        economic_top5_share=_share(sum(value for _, value in ranked[:5]), supply),
        economic_top10_share=_share(sum(value for _, value in ranked[:TOP_N]), supply),
        economic_top_holders=tuple(
            EconomicHolder(
                holder=holder,
                balance_raw=value,
                share=_share(value, supply),
                is_burn_address=holder in BURN_ADDRESSES,
            )
            for holder, value in ranked[:TOP_N]
        ),
        **figures,  # type: ignore[arg-type]
    )


# The control buckets recorded beside the distribution, by their field stem.
BUCKET_FIELDS: dict[PositionControlState, str] = {
    PositionControlState.PERMANENTLY_LOCKED: "permanently_locked",
    PositionControlState.TIMELOCKED: "timelocked",
    PositionControlState.RELEASABLE: "releasable",
    PositionControlState.UNKNOWN_CONTRACT_CUSTODY: "unknown_custody",
}


def _control_buckets(attributed: list[V4PositionFacts]) -> dict[PositionControlState, int]:
    """Token supply per control state, over every position with control facts."""
    buckets = dict.fromkeys(PositionControlState, 0)
    for item in attributed:
        if item.control is not None:
            buckets[item.control.control_state] += item.controlled_token_raw
    return buckets


def _retained(holders: HolderFacts, census: V4Census) -> dict[str, int]:
    """The raw distribution by address, without the PoolManager's own row.

    Every unit of the PoolManager row is re-assigned below or remains explicitly
    unattributed or locked, so counting the row as well would count it twice.
    """
    return {
        row.address: row.balance_raw
        for row in holders.top_holders
        if row.address != census.pool_manager
    }


def _ranked(
    holders: HolderFacts,
    census: V4Census,
    controlled: list[tuple[str, int]],
    *,
    unseen: int,
    unattributed_raw: int,
) -> list[tuple[str, int]]:
    """Holders ranked by what they control, largest first, ties by address.

    Permanently locked supply is in no holder's row: it is not anyone's to
    move. A controller outside the retained prefix is credited with ``unseen``
    on top of its position -- the most it could hold unseen for an upper
    bound, nothing for a floor.
    """
    balances = _retained(holders, census)
    for owner, amount in controlled:
        if owner not in balances:
            balances[owner] = unseen
        balances[owner] += amount
    if unattributed_raw:
        balances[UNATTRIBUTED_POOL_BALANCE] = unattributed_raw
    return sorted(balances.items(), key=lambda pair: (-pair[1], pair[0]))
