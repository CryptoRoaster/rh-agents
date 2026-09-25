"""A position mark is a recorded market price, at the precision it was recorded.

The mark is provenance about an observation, so it keeps the market layer's
precision. The accounting boundary is the valuation result: exposure and
unrealized loss are computed from quantity times the exact mark, and only that
USD result is brought to the ledger's eighteen places, rounded up because a
smaller exposure or loss would be more permissive for SENTINEL. A result the
ledger cannot hold at all is a typed accounting gap, never a crash.
"""

import json
from datetime import timedelta
from decimal import Decimal, localcontext
from uuid import uuid4

from src.core.numbers import quantize, quantize_up
from src.ledger.portfolio import portfolio_basis, portfolio_state, replay_portfolio_basis
from src.orchestration.valuation.service import PositionValuationReader
from tests.casefill.conftest import MultiMarkets
from tests.riskdata.conftest import BASE_ASSET, market_for, recorded_snapshot
from tests.valuation.test_marks import holding

# Synthetic, 23 decimal places.
WIDE_MARK = Decimal("1.25000000000000000000123")
QUANTITY = Decimal("123456.123456789012345678")
OTHER = market_for(token="c7" * 20, pool="d8" * 20)


async def wide_valuation(now):
    feed = MultiMarkets(recorded_snapshot(now, age=timedelta(seconds=5), price=WIDE_MARK))
    reader = PositionValuationReader(markets=feed, max_age_seconds=30)
    return await reader.value([holding(now, quantity=str(QUANTITY))], now)


async def test_a_wide_recorded_price_becomes_an_exact_mark(now) -> None:
    valuation = await wide_valuation(now)
    assert valuation.complete
    mark = valuation.by_asset[BASE_ASSET]
    assert mark.price_usd == WIDE_MARK
    assert mark.price_usd.as_tuple() == WIDE_MARK.as_tuple()
    assert isinstance(mark.price_usd, Decimal)


async def test_the_serialized_mark_loses_no_precision(now) -> None:
    mark = (await wide_valuation(now)).by_asset[BASE_ASSET]
    dumped = mark.model_dump(mode="json")
    assert dumped["price_usd"] == "1.25000000000000000000123"
    assert type(mark).model_validate(json.loads(json.dumps(dumped))).price_usd == WIDE_MARK


async def test_exposure_is_quantity_times_the_exact_mark_then_quantized(now) -> None:
    mark = (await wide_valuation(now)).by_asset[BASE_ASSET]
    held = holding(now, quantity=str(QUANTITY))
    # The holding is valued from its mark: the order is for a different asset.
    state = portfolio_state(
        cash_usd=Decimal("10000"),
        realized_loss_today_usd=Decimal("0"),
        positions=[held],
        asset_id=OTHER.base_asset_id,
        price_usd=Decimal("2"),
        marks={BASE_ASSET: mark},
        now=now,
        max_snapshot_age_seconds=30,
        correlation_id=uuid4(),
        market=OTHER,
    )
    with localcontext() as exact:
        exact.prec = 200
        # Rounded up: a smaller exposure would be more permissive for SENTINEL.
        expected = quantize_up(QUANTITY * WIDE_MARK)
    assert state.marks_used == (mark,)
    assert state.prices[BASE_ASSET] == WIDE_MARK
    assert state.context.exposure_usd == expected
    # The product is quantized once, at the end: not the price first.
    assert state.context.exposure_usd != quantize_up(QUANTITY * quantize(WIDE_MARK))


async def test_the_stored_basis_replays_to_the_same_state(now) -> None:
    mark = (await wide_valuation(now)).by_asset[BASE_ASSET]
    held = holding(now, quantity=str(QUANTITY))
    state = portfolio_state(
        cash_usd=Decimal("10000"),
        realized_loss_today_usd=Decimal("0"),
        positions=[held],
        asset_id=OTHER.base_asset_id,
        price_usd=Decimal("2"),
        marks={BASE_ASSET: mark},
        now=now,
        max_snapshot_age_seconds=30,
        correlation_id=uuid4(),
        market=OTHER,
    )
    stored = json.loads(json.dumps(portfolio_basis(state)))
    replayed = replay_portfolio_basis(stored)
    assert replayed.context == state.context
    assert replayed.marks_used == state.marks_used
    assert replayed.marks_used[0].price_usd == WIDE_MARK


# ---------------------------------------------- accounting result representability


def mark_for(now, asset_id: str, price: Decimal):
    from src.orchestration.valuation.models import PositionMark

    return PositionMark(
        asset_id=asset_id,
        pair_id=f"pair-for-{asset_id[-6:]}",
        provider="geckoterminal",
        snapshot_id=uuid4(),
        observation_id=uuid4(),
        price_usd=price,
        observed_at=now - timedelta(seconds=5),
    )


THIRD_ASSET = market_for(token="e9" * 20, pool="fa" * 20).base_asset_id


def state_for(now, holdings, marks, *, realized=Decimal("0")):
    return portfolio_state(
        cash_usd=Decimal("10000"),
        realized_loss_today_usd=realized,
        positions=holdings,
        asset_id=OTHER.base_asset_id,
        price_usd=Decimal("2"),
        marks=marks,
        now=now,
        max_snapshot_age_seconds=30,
        correlation_id=uuid4(),
        market=OTHER,
    )


def assert_accounting_gap(state, issue: str) -> None:
    from src.core.models import SafetyStatus

    assert issue in {item.value for item in state.accounting_issues}
    assert state.context.accounting is SafetyStatus.UNKNOWN
    assert state.context.exposure_usd is None
    assert state.context.daily_loss_usd is None
    # The marks were there: this is not a missing price.
    assert state.unmarked_assets == ()


def test_a_single_position_worth_more_than_the_ledger_holds_is_a_typed_gap(now) -> None:
    huge = Decimal("1E+25")
    mark = mark_for(now, BASE_ASSET, huge)
    state = state_for(now, [holding(now, quantity="1")], {BASE_ASSET: mark})
    assert_accounting_gap(state, "EXPOSURE_OUTSIDE_ACCOUNTING_PRECISION")
    # Fully auditable: the exact mark and the inputs survive.
    assert state.marks_used == (mark,)
    assert state.prices[BASE_ASSET] == huge
    assert mark.price_usd == huge


def test_two_representable_positions_can_sum_past_the_ledger(now) -> None:
    each = Decimal("60000000000000000000")  # 6E+19: twenty integer digits
    holdings = [
        holding(now, quantity="1"),
        holding(now, asset_id=THIRD_ASSET, pair_id="third-pair", quantity="1"),
    ]
    marks = {
        BASE_ASSET: mark_for(now, BASE_ASSET, each),
        THIRD_ASSET: mark_for(now, THIRD_ASSET, each),
    }
    state = state_for(now, holdings, marks)
    assert_accounting_gap(state, "EXPOSURE_OUTSIDE_ACCOUNTING_PRECISION")


def test_an_unrealized_loss_past_the_ledger_is_a_typed_gap(now) -> None:
    cost = Decimal("60000000000000000000")
    holdings = [
        holding(now, quantity="1", cost_basis_usd=cost),
        holding(now, asset_id=THIRD_ASSET, pair_id="third-pair", quantity="1", cost_basis_usd=cost),
    ]
    tiny = Decimal("0.01")
    marks = {
        BASE_ASSET: mark_for(now, BASE_ASSET, tiny),
        THIRD_ASSET: mark_for(now, THIRD_ASSET, tiny),
    }
    state = state_for(now, holdings, marks)
    assert_accounting_gap(state, "DAILY_LOSS_OUTSIDE_ACCOUNTING_PRECISION")


def test_realized_plus_unrealized_loss_past_the_ledger_is_a_typed_gap(now) -> None:
    cost = Decimal("60000000000000000000")
    holdings = [holding(now, quantity="1", cost_basis_usd=cost)]
    marks = {BASE_ASSET: mark_for(now, BASE_ASSET, Decimal("0.01"))}
    state = state_for(now, holdings, marks, realized=Decimal("60000000000000000000"))
    assert_accounting_gap(state, "DAILY_LOSS_OUTSIDE_ACCOUNTING_PRECISION")


def test_a_wide_but_representable_portfolio_has_no_gap(now) -> None:
    from src.core.models import SafetyStatus

    mark = mark_for(now, BASE_ASSET, WIDE_MARK)
    state = state_for(now, [holding(now, quantity=str(QUANTITY))], {BASE_ASSET: mark})
    assert state.accounting_issues == ()
    assert state.context.accounting is SafetyStatus.PASS
    assert state.context.exposure_usd is not None


def test_exposure_and_loss_are_never_rounded_below_the_exact_value(now) -> None:
    """A smaller exposure or loss is more permissive for SENTINEL, so round up.

    SENTINEL rejects when `exposure + worst_cost > max_exposure_usd` and when
    `daily_loss >= daily_loss_limit_usd`; a figure rounded down could pass either.
    """
    above = Decimal("1.0000000000000000004")  # half-even would give 1.000000000000000000
    state = state_for(
        now, [holding(now, quantity="1")], {BASE_ASSET: mark_for(now, BASE_ASSET, above)}
    )
    assert state.context.exposure_usd is not None
    assert state.context.exposure_usd >= above
    assert state.context.exposure_usd - above < Decimal("0.000000000000000001")

    losing = Decimal("1.0000000000000000006")  # loss 0.9999999999999999994
    state = state_for(
        now,
        [holding(now, quantity="1", cost_basis_usd=Decimal("2"))],
        {BASE_ASSET: mark_for(now, BASE_ASSET, losing)},
    )
    exact_loss = Decimal("2") - losing
    assert state.context.daily_loss_usd is not None
    assert state.context.daily_loss_usd >= exact_loss
    assert state.context.daily_loss_usd - exact_loss < Decimal("0.000000000000000001")
