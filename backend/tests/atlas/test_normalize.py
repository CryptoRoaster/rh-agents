"""Exact holder mathematics, proven without a network.

Concentration is a safety metric, so every number here is computed from raw
integer balances with Decimal. A float never appears, and no provider-supplied
percentage is ever trusted over arithmetic we can do ourselves.
"""

from decimal import Decimal

import pytest

from src.agents.atlas.models import (
    AtlasSourceFailure,
    HolderCompleteness,
    HolderSourceRow,
)
from src.agents.atlas.sources.normalize import (
    HolderNormalizationError,
    concentration,
    is_descending,
    ordered_rows,
)
from tests.atlas.conftest import BURN

SUPPLY = 10**24
UINT256_MAX = 2**256 - 1


def row(index: int, balance: int, *, address: str | None = None) -> HolderSourceRow:
    return HolderSourceRow(address=address or ("0x" + f"{index:02x}" * 20), balance_raw=balance)


def ladder(
    count: int, *, top: int = 5 * 10**22, step: int = 10**21, start: int = 16
) -> tuple[HolderSourceRow, ...]:
    """A descending run of rows whose addresses never collide with the explicit ones."""
    return tuple(row(start + index, top - index * step) for index in range(count))


def test_top_shares_are_exact_decimals_over_on_chain_supply():
    rows = (row(1, 4 * 10**23), row(2, 3 * 10**23), *ladder(8, top=10**22))
    measured = concentration(rows, SUPPLY, HolderCompleteness.TOP_N_ONLY)
    assert measured.top1_share == Decimal("0.4")
    assert measured.top5_share == Decimal("0.727")
    assert measured.top10_share == Decimal("0.752")
    assert all(isinstance(share.share, Decimal) for share in measured.shares)


def test_a_provider_percentage_can_never_override_the_exact_calculation():
    """A vendor claiming 20% does not change what the balances actually say."""
    rows = (row(1, 31 * 10**22), *ladder(9, top=10**20, step=10**18))
    measured = concentration(rows, SUPPLY, HolderCompleteness.TOP_N_ONLY)
    assert measured.top1_share == Decimal("0.31")
    assert measured.top1_share != Decimal("0.20")


def test_uint256_scale_balances_do_not_overflow_or_lose_precision():
    """Full-width uint256 arithmetic stays exact where it matters.

    Shares are quantized to eighteen places, so a ratio this extreme saturates at
    1 — which overstates concentration rather than understating it, the safe
    direction for a risk metric. The raw integers themselves stay exact.
    """
    rows = (row(1, UINT256_MAX - 100), *ladder(9, top=1, step=0))
    measured = concentration(rows, UINT256_MAX, HolderCompleteness.TOP_N_ONLY)
    assert measured.top1_share <= Decimal(1)
    assert measured.top10_share <= Decimal(1)
    assert measured.shares[0].balance_raw == UINT256_MAX - 100
    # A ratio that eighteen places can represent is reproduced exactly.
    assert concentration(
        (row(1, UINT256_MAX // 4), *ladder(9, top=1, step=0)),
        UINT256_MAX,
        HolderCompleteness.TOP_N_ONLY,
    ).top1_share == Decimal("0.25")


def test_rows_are_sorted_by_balance_and_tie_broken_by_address():
    unordered = (row(3, 10), row(1, 500), row(2, 10))
    assert [item.balance_raw for item in ordered_rows(unordered)] == [500, 10, 10]
    assert [item.address for item in ordered_rows(unordered)][1:] == [
        "0x" + "02" * 20,
        "0x" + "03" * 20,
    ]


def test_an_unordered_provider_page_is_still_measured_correctly():
    """Ordering is never assumed; it is imposed before any top-N is taken."""
    ascending = tuple(reversed(ladder(12)))
    assert is_descending(ascending) is False
    measured = concentration(ascending, SUPPLY, HolderCompleteness.COMPLETE)
    assert measured.shares[0].balance_raw == 5 * 10**22


def test_a_duplicated_holder_address_is_refused_rather_than_summed():
    duplicated = (row(1, 10**22), row(1, 10**22))
    with pytest.raises(HolderNormalizationError) as error:
        concentration(duplicated, SUPPLY, HolderCompleteness.COMPLETE)
    assert error.value.failure == AtlasSourceFailure.INVALID_RESPONSE


@pytest.mark.parametrize("supply", [None, 0])
def test_without_an_on_chain_denominator_no_share_can_be_stated(supply):
    with pytest.raises(HolderNormalizationError) as error:
        concentration(ladder(12), supply, HolderCompleteness.COMPLETE)
    assert error.value.failure == AtlasSourceFailure.DENOMINATOR_UNKNOWN


def test_holders_cannot_collectively_hold_more_than_exists():
    with pytest.raises(HolderNormalizationError) as error:
        concentration((row(1, SUPPLY + 1),), SUPPLY, HolderCompleteness.COMPLETE)
    assert error.value.failure == AtlasSourceFailure.SUPPLY_INCONSISTENT


def test_unproven_coverage_yields_no_metric_at_all():
    with pytest.raises(HolderNormalizationError) as error:
        concentration(ladder(12), SUPPLY, HolderCompleteness.UNKNOWN)
    assert error.value.failure == AtlasSourceFailure.INCOMPLETE_RESULT


def test_a_prefix_shorter_than_ten_cannot_support_a_top_ten_share():
    with pytest.raises(HolderNormalizationError) as error:
        concentration(ladder(9), SUPPLY, HolderCompleteness.TOP_N_ONLY)
    assert error.value.failure == AtlasSourceFailure.INCOMPLETE_RESULT


def test_a_complete_set_smaller_than_ten_is_perfectly_measurable():
    measured = concentration(ladder(3), SUPPLY, HolderCompleteness.COMPLETE)
    assert measured.top10_share == measured.top5_share


def test_raw_concentration_never_hides_a_burn_address_or_a_pool():
    pool = row(1, 5 * 10**23)
    rows = (
        pool,
        HolderSourceRow(address=BURN, balance_raw=2 * 10**23),
        *ladder(10, top=10**21, step=10**19),
    )
    measured = concentration(rows, SUPPLY, HolderCompleteness.COMPLETE)
    # The raw metric counts everything, so a dominant pool stays visible.
    assert measured.top1_share == Decimal("0.5")
    assert measured.top10_share > Decimal("0.7")
    assert any(share.is_burn_address for share in measured.shares)
    # The adjustment is reported beside the raw number, never instead of it.
    assert measured.burned_raw == 2 * 10**23
    assert measured.burned_share == Decimal("0.2")
    assert measured.top10_share_excluding_burn is not None
    assert measured.top10_share_excluding_burn != measured.top10_share


def test_a_burn_adjustment_is_withheld_when_only_a_prefix_was_seen():
    """From a prefix the burned amount is a lower bound, not a denominator."""
    measured = concentration(ladder(12), SUPPLY, HolderCompleteness.TOP_N_ONLY)
    assert measured.burned_raw is None
    assert measured.burned_share is None
    assert measured.top10_share_excluding_burn is None


def test_an_empty_holder_set_against_a_live_supply_is_impossible_not_safe():
    with pytest.raises(HolderNormalizationError) as error:
        concentration((), SUPPLY, HolderCompleteness.COMPLETE)
    assert error.value.failure == AtlasSourceFailure.SUPPLY_INCONSISTENT
