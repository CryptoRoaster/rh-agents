"""Provider-neutral holder mathematics. No vendor detail and no network here.

Every concentration ATLAS uses is computed in this module from raw integer
balances and the on-chain total supply. A provider's own percentage is never
authoritative: it is derived from data we already have, so deriving it ourselves
removes a whole class of silent vendor disagreement.

Money and ratios never pass through a float.
"""

from dataclasses import dataclass
from decimal import Decimal, localcontext

from src.agents.atlas.models import (
    BURN_ADDRESSES,
    AtlasSourceFailure,
    HolderCompleteness,
    HolderShare,
    HolderSourceRow,
)
from src.core.numbers import quantize

# How many ordered rows must be present before a top-10 share can be claimed
# from a prefix rather than from the complete holder set.
TOP_N = 10
# Bounded audit trail kept on the fact record; well under the model's cap.
RETAINED_HOLDERS = 20


class HolderNormalizationError(Exception):
    """Untrusted provider data could not be turned into a usable fact."""

    def __init__(self, failure: AtlasSourceFailure) -> None:
        self.failure = failure
        super().__init__(failure.value)


@dataclass(frozen=True)
class HolderConcentration:
    """Exact raw concentration, plus the burn-adjusted view when it is provable."""

    top1_share: Decimal
    top5_share: Decimal
    top10_share: Decimal
    shares: tuple[HolderShare, ...]
    burned_raw: int | None
    burned_share: Decimal | None
    top10_share_excluding_burn: Decimal | None


def ordered_rows(rows: tuple[HolderSourceRow, ...]) -> tuple[HolderSourceRow, ...]:
    """Deterministically ordered rows, largest balance first.

    Provider ordering is never assumed. Equal balances tie-break on the canonical
    address so the same input always produces the same top-N and the same digest.
    """
    seen: set[str] = set()
    for row in rows:
        if row.address in seen:
            # Summing duplicate rows would silently invent a balance. A provider
            # that repeats an address has returned something we cannot interpret.
            raise HolderNormalizationError(AtlasSourceFailure.INVALID_RESPONSE)
        seen.add(row.address)
    return tuple(sorted(rows, key=lambda row: (-row.balance_raw, row.address)))


def is_descending(rows: tuple[HolderSourceRow, ...]) -> bool:
    """Whether the provider actually delivered the balance order it promises."""
    return all(
        rows[index].balance_raw >= rows[index + 1].balance_raw for index in range(len(rows) - 1)
    )


def _share(amount: int, denominator: int) -> Decimal:
    with localcontext() as context:
        context.prec = 78
        return quantize(Decimal(amount) / Decimal(denominator))


def concentration(
    rows: tuple[HolderSourceRow, ...],
    total_supply_raw: int | None,
    completeness: HolderCompleteness,
    excluded_addresses: tuple[str, ...] = (),
) -> HolderConcentration:
    """Raw top-N concentration against on-chain supply.

    Nothing is excluded *here* — not liquidity pools, not burn addresses, not the
    deployer. A large position stays visible as a large position, and any
    adjustment is reported beside it rather than instead of it.

    ``excluded_addresses`` names what the **provider** removed before we ever saw
    the rows. It changes no raw figure, because a row that never arrived cannot
    be added back; it only withholds the burn adjustment when a burn address is
    among the exclusions, since a burn total computed without them is a lower
    bound and a lower bound used as a denominator adjustment understates
    concentration.
    """
    if total_supply_raw is None or total_supply_raw <= 0:
        raise HolderNormalizationError(AtlasSourceFailure.DENOMINATOR_UNKNOWN)
    ordered = ordered_rows(rows)
    if completeness == HolderCompleteness.UNKNOWN:
        raise HolderNormalizationError(AtlasSourceFailure.INCOMPLETE_RESULT)
    if completeness == HolderCompleteness.TOP_N_ONLY and len(ordered) < TOP_N:
        # A prefix shorter than the metric it is supposed to support proves
        # nothing about the top ten.
        raise HolderNormalizationError(AtlasSourceFailure.INCOMPLETE_RESULT)
    if not ordered:
        # Supply exists but nobody holds it: arithmetically impossible for a
        # standard token, so this is a broken source rather than a safe zero.
        raise HolderNormalizationError(AtlasSourceFailure.SUPPLY_INCONSISTENT)
    if sum(row.balance_raw for row in ordered) > total_supply_raw:
        # Holders cannot collectively hold more than exists.
        raise HolderNormalizationError(AtlasSourceFailure.SUPPLY_INCONSISTENT)

    top1 = _share(ordered[0].balance_raw, total_supply_raw)
    top5 = _share(sum(row.balance_raw for row in ordered[:5]), total_supply_raw)
    top10 = _share(sum(row.balance_raw for row in ordered[:TOP_N]), total_supply_raw)

    burned_raw: int | None = None
    burned_share: Decimal | None = None
    adjusted: Decimal | None = None
    if completeness == HolderCompleteness.COMPLETE and not (
        BURN_ADDRESSES & set(excluded_addresses)
    ):
        # Only a complete holder set that could actually contain the burn sinks
        # proves how much is burned. From a prefix — or from a provider that
        # filters a burn address out server-side — the burned amount is a lower
        # bound, and a lower bound presented as a denominator adjustment would
        # understate concentration.
        burned_raw = sum(row.balance_raw for row in ordered if row.address in BURN_ADDRESSES)
        burned_share = _share(burned_raw, total_supply_raw)
        circulating = total_supply_raw - burned_raw
        if circulating > 0:
            remaining = [row for row in ordered if row.address not in BURN_ADDRESSES]
            adjusted = _share(sum(row.balance_raw for row in remaining[:TOP_N]), circulating)

    shares = tuple(
        HolderShare(
            address=row.address,
            balance_raw=row.balance_raw,
            share=_share(row.balance_raw, total_supply_raw),
            is_burn_address=row.address in BURN_ADDRESSES,
            is_contract=row.is_contract,
        )
        for row in ordered[:RETAINED_HOLDERS]
    )
    return HolderConcentration(
        top1_share=top1,
        top5_share=top5,
        top10_share=top10,
        shares=shares,
        burned_raw=burned_raw,
        burned_share=burned_share,
        top10_share_excluding_burn=adjusted,
    )
