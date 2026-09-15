"""Reproductions for the 2M-E review findings, then the proofs they are closed.

Three defects, each written here before it was fixed:

1. A holding of the asset the case is for was skipped by `portfolio_state` and
   priced at the case's own market price — so inventory bought in pool A was
   valued at pool B's price, and a holding with no recorded market was valued as
   if it had been bought in the case's. `covers()` could not catch either,
   because it compared *which* assets were looked at, not which were priced.
2. The pre-lock replay short-circuit only knew about completed fills, so a
   stored fill-time rejection still tried to value the portfolio and failed when
   the market layer was unreachable.
3. The stored basis kept marks and aggregate figures but not the holdings they
   applied to, so nobody could recompute the exposure SENTINEL judged.
"""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from src.core.models import Position, RiskContext, RiskLimits
from src.data.repository import save_position
from src.data.tables import ExecutionRow, PositionRow, TradeCaseExecutionRow
from src.ledger.portfolio import portfolio_state, replay_portfolio_basis
from src.orchestration.casefill.models import ExecutionRefusal
from src.orchestration.riskrequest.models import RiskRequestRefusal
from src.orchestration.valuation.models import (
    PortfolioValuation,
    UnvaluedPosition,
    ValuationRefusal,
)
from tests.casefill.conftest import MultiMarkets, approved_case, build_fill_service
from tests.riskdata.conftest import BASE_ASSET, IDENTITY, PAIR_ID, market_for, recorded_snapshot
from tests.riskrequest.conftest import FRESH, build_service, read_account, ready_case

# The same asset, in a different pool. Only the pool moves: an asset trades in
# many markets, and that is the whole ambiguity this phase refuses to resolve
# by picking one.
OTHER_POOL = IDENTITY.model_copy(
    update={"pair_id": f"{IDENTITY.chain}:{IDENTITY.network}:contract_address:0x{'ab' * 20}"}
)
# A different asset entirely, in its own market. The ordinary case: a holding
# somewhere else, valued from its own market while this case runs.
ELSEWHERE = market_for(token="c7" * 20, pool="d8" * 20)


class Unreachable:
    """A market layer that fails on contact. Replay must never touch it."""

    def __init__(self) -> None:
        self.reads = 0

    async def latest(self, identity, *, include_fixtures=False):
        self.reads += 1
        raise AssertionError(f"the market layer was read for {identity}")


def two_pools(now, *, other_price=Decimal("9"), elsewhere_price=Decimal("9")):
    """This case's market, a second pool trading the very same asset, and a
    third market holding a different asset altogether."""
    primary = recorded_snapshot(now, age=FRESH, metadata_age=FRESH)
    other = recorded_snapshot(
        now,
        age=FRESH,
        metadata_age=FRESH,
        base_asset_id=BASE_ASSET,
        pair_id=OTHER_POOL.pair_id,
        label="otherpool",
        price=other_price,
    )
    elsewhere = recorded_snapshot(
        now,
        age=FRESH,
        metadata_age=FRESH,
        base_asset_id=ELSEWHERE.base_asset_id,
        pair_id=ELSEWHERE.pair_id,
        label="elsewhere",
        price=elsewhere_price,
    )
    return MultiMarkets(primary, other, elsewhere), primary, other


async def positions_now(sessions):
    async with sessions() as session:
        return (await session.scalars(select(PositionRow))).all()


async def counts(sessions):
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(ExecutionRow))


async def hold(sessions, now, trace, *, market, quantity="4", cost="100", asset=None):
    """A position placed directly, to stand for one acquired earlier."""
    async with sessions.begin() as session:
        await save_position(
            session,
            Position(
                source="LEDGER",
                correlation_id=trace,
                asset_id=asset or BASE_ASSET,
                market_pair_id=None if market is None else market.pair_id,
                market_chain=None if market is None else market.chain,
                market_network=None if market is None else market.network,
                market_provider=None if market is None else market.provider,
                quantity=Decimal(quantity),
                cost_basis_usd=Decimal(cost),
                created_at=now,
                updated_at=now,
            ),
        )


async def move_the_world_on(sessions, asset):
    """Change the position rows a reconstruction must not be reading."""
    async with sessions.begin() as session:
        await session.execute(
            update(PositionRow)
            .where(PositionRow.asset_id == asset)
            .values(quantity=Decimal("99"), cost_basis_usd=Decimal("4242"))
        )


# ============================================================ finding 1


async def test_a_holding_in_another_pool_is_not_priced_by_this_case(risk_db, now, trace):
    """Inventory from pool A valued at pool B's price is wrong by the spread.

    Reproduction: the holding is the case's own asset, so `portfolio_state`
    skipped it and `prices[asset_id]` — this market's price — priced it anyway.
    """
    _, sessions = risk_db
    feed, _, _ = two_pools(now)
    case, _, _ = await approved_case(sessions, now, uuid4(), key="pool", feed=feed)
    await hold(sessions, now, trace, market=OTHER_POOL)

    service = build_fill_service(sessions, now, feed=feed)
    result = await service.execute_case_fill(case.id, request_key="pool-req")

    assert result.kind == "execution_refused"
    assert result.reason is ExecutionRefusal.POSITION_MARKET_CONFLICT
    assert await counts(sessions) == 0


async def test_a_risk_request_refuses_an_asset_already_held_in_another_pool(risk_db, now, trace):
    """The approval stops too: an order that could never be filled is not one."""
    _, sessions = risk_db
    feed, _, _ = two_pools(now)
    await hold(sessions, now, trace, market=OTHER_POOL)

    risk = build_service(sessions, now, feed=feed)
    case = await ready_case(risk.cases, now, uuid4(), key="pool-request")
    result = await risk.request_risk_evaluation(case.id, request_key="pool-request-req")

    assert result.kind == "risk_request_refused"
    assert result.reason is RiskRequestRefusal.POSITION_MARKET_CONFLICT


async def test_a_holding_without_a_recorded_market_is_never_the_case_s_market(risk_db, now, trace):
    """A missing market identity is a refusal, not an invitation to assume one."""
    _, sessions = risk_db
    feed, _, _ = two_pools(now)
    case, _, _ = await approved_case(sessions, now, uuid4(), key="blank", feed=feed)
    await hold(sessions, now, trace, market=None)

    service = build_fill_service(sessions, now, feed=feed)
    result = await service.execute_case_fill(case.id, request_key="blank-req")

    assert result.kind == "execution_refused"
    assert result.reason is ExecutionRefusal.PORTFOLIO_MARKS_UNAVAILABLE
    assert result.detail == ValuationRefusal.POSITION_MARKET_UNKNOWN.value
    assert await counts(sessions) == 0


async def test_a_risk_request_refuses_a_holding_without_a_recorded_market(risk_db, now, trace):
    _, sessions = risk_db
    feed, _, _ = two_pools(now)
    await hold(sessions, now, trace, market=None)

    risk = build_service(sessions, now, feed=feed)
    case = await ready_case(risk.cases, now, uuid4(), key="blank-request")
    result = await risk.request_risk_evaluation(case.id, request_key="blank-request-req")

    assert result.kind == "risk_request_refused"
    assert result.reason is RiskRequestRefusal.PORTFOLIO_MARKS_UNAVAILABLE
    assert result.detail == ValuationRefusal.POSITION_MARKET_UNKNOWN.value


def test_a_refused_holding_does_not_count_as_valued():
    """`covers()` compared which assets were looked at, not which were priced."""
    valuation = PortfolioValuation(
        unvalued=(UnvaluedPosition(asset_id="a", reason=ValuationRefusal.PRICE_STALE),),
        considered_assets=("a",),
    )

    assert valuation.unconsidered({"a"}) == ()
    assert valuation.unusable({"a"}) == ("a",)
    assert valuation.complete is False


def test_a_holding_in_this_market_is_priced_by_this_market_s_own_reading(now):
    """The position's own market happens to be the one being judged.

    Not a substitution: it is the same market, read under the same freshness
    bound. What may never happen is the reverse — another market's holding, or a
    holding with no market at all, taking this price.
    """
    here = portfolio_state(
        cash_usd=Decimal("1000"),
        realized_loss_today_usd=Decimal("0"),
        positions=[
            Position(
                source="LEDGER",
                correlation_id=uuid4(),
                asset_id=BASE_ASSET,
                market_pair_id=PAIR_ID,
                market_chain=IDENTITY.chain,
                market_network=IDENTITY.network,
                market_provider=IDENTITY.provider,
                quantity=Decimal("4"),
                cost_basis_usd=Decimal("100"),
                created_at=now,
                updated_at=now,
            )
        ],
        asset_id=BASE_ASSET,
        price_usd=Decimal("2"),
        marks=None,
        now=now,
        max_snapshot_age_seconds=30,
        correlation_id=uuid4(),
        market=IDENTITY,
    )

    assert here.unmarked_assets == ()
    assert here.conflicting_market is None
    assert here.context.exposure_usd == Decimal("8")


def test_a_different_asset_is_never_priced_by_this_market(now):
    """A market prices one base asset. The other holding needs its own mark."""
    other = portfolio_state(
        cash_usd=Decimal("1000"),
        realized_loss_today_usd=Decimal("0"),
        positions=[
            Position(
                source="LEDGER",
                correlation_id=uuid4(),
                asset_id=ELSEWHERE.base_asset_id,
                quantity=Decimal("4"),
                cost_basis_usd=Decimal("100"),
                created_at=now,
                updated_at=now,
            )
        ],
        asset_id=BASE_ASSET,
        price_usd=Decimal("2"),
        marks=None,
        now=now,
        max_snapshot_age_seconds=30,
        correlation_id=uuid4(),
        market=None,
    )

    assert other.unmarked_assets == (ELSEWHERE.base_asset_id,)
    assert other.context.exposure_usd is None
    assert other.context.accounting.value == "UNKNOWN"


# ============================================================ finding 2


async def test_a_stored_fill_rejection_replays_without_reading_the_market(risk_db, now, trace):
    """History needs no prices, and a rejection is history too.

    Reproduction: the short-circuit before the valuation only knew about
    completed fills, so a stored rejection valued the portfolio again — and with
    a holding to price and an unreachable market layer, that raised.
    """
    from tests.riskrequest.conftest import set_account

    _, sessions = risk_db
    feed, _, _ = two_pools(now)
    await hold(sessions, now, trace, market=ELSEWHERE, asset=ELSEWHERE.base_asset_id)
    case, _, _ = await approved_case(sessions, now, uuid4(), key="rej", feed=feed)

    await set_account(sessions, cash_usd=Decimal("10"))
    first = await build_fill_service(sessions, now, feed=feed).execute_case_fill(
        case.id, request_key="rej-req"
    )
    assert first.reason is ExecutionRefusal.RISK_RECHECK_REFUSED
    assert "INSUFFICIENT_CASH" in first.reason_codes
    before = await read_account(sessions)

    offline = Unreachable()
    again = await build_fill_service(sessions, now, feed=offline).execute_case_fill(
        case.id, request_key="rej-req"
    )

    assert again.kind == "execution_refused"
    assert again.reason is ExecutionRefusal.RISK_RECHECK_REFUSED
    assert again.replayed is True
    assert again.reason_codes == first.reason_codes
    assert offline.reads == 0
    assert await counts(sessions) == 0
    assert (await read_account(sessions)).cash_usd == before.cash_usd


async def test_a_completed_fill_still_replays_and_a_wrong_key_is_still_refused(risk_db, now, trace):
    """The widened short-circuit did not widen what replays as a success."""
    _, sessions = risk_db
    feed, _, _ = two_pools(now)
    case, _, _ = await approved_case(sessions, now, trace, key="keep", feed=feed)
    service = build_fill_service(sessions, now, feed=feed)
    filled = await service.execute_case_fill(case.id, request_key="keep-req")
    assert filled.kind == "paper_fill_recorded"

    offline = build_fill_service(sessions, now, feed=Unreachable())
    again = await offline.execute_case_fill(case.id, request_key="keep-req")
    assert again.replayed is True
    assert again.execution_id == filled.execution_id

    wrong = await offline.execute_case_fill(case.id, request_key="someone-else")
    assert wrong.reason is ExecutionRefusal.REQUEST_KEY_MISMATCH


# ============================================================ finding 3


def rebuilt(stored: dict) -> RiskContext:
    """Recompute the portfolio from the stored basis alone.

    Through the same `portfolio_state` the decision used. Nothing is read from
    the current position rows, and no figure is taken from the record: the
    exposure comes out of the arithmetic again.
    """
    return replay_portfolio_basis(stored).context


async def test_the_fill_s_stored_basis_recomputes_what_sentinel_judged(risk_db, now, trace):
    """Reproduction: the execution basis kept marks and no holdings at all."""
    _, sessions = risk_db
    feed, _, _ = two_pools(now)
    await hold(sessions, now, trace, market=ELSEWHERE, asset=ELSEWHERE.base_asset_id, quantity="4")
    case, _, _ = await approved_case(sessions, now, uuid4(), key="audit", feed=feed)

    service = build_fill_service(sessions, now, feed=feed)
    result = await service.execute_case_fill(case.id, request_key="audit-req")
    assert result.kind == "paper_fill_recorded", getattr(result, "detail", None)

    async with sessions() as session:
        row = await session.scalar(
            select(TradeCaseExecutionRow).where(TradeCaseExecutionRow.trade_case_id == case.id)
        )
    stored = row.basis["portfolio"]

    # The world moves on: the holdings change and the market layer goes away.
    await move_the_world_on(sessions, ELSEWHERE.base_asset_id)

    recomputed = rebuilt(stored)
    judged = RiskContext.model_validate(row.basis["risk_context"])
    assert recomputed == judged
    assert recomputed.exposure_usd == Decimal("36.000000000000000000")  # 4 x 9, pool A's price
    assert recomputed.accounting.value == "PASS"


async def test_the_request_s_stored_basis_recomputes_what_sentinel_judged(risk_db, now, trace):
    """The same for the approval, whose basis records the same portfolio."""
    from src.data.tables import TradeCaseRiskRequestRow

    _, sessions = risk_db
    feed, _, _ = two_pools(now)
    await hold(sessions, now, trace, market=ELSEWHERE, asset=ELSEWHERE.base_asset_id, quantity="4")

    risk = build_service(sessions, now, feed=feed)
    case = await ready_case(risk.cases, now, uuid4(), key="audit-request")
    result = await risk.request_risk_evaluation(case.id, request_key="audit-request-req")
    assert result.kind == "risk_request_evaluated", getattr(result, "reason", None)

    async with sessions() as session:
        row = await session.scalar(
            select(TradeCaseRiskRequestRow).where(TradeCaseRiskRequestRow.trade_case_id == case.id)
        )
    stored = row.basis["portfolio"]

    await move_the_world_on(sessions, ELSEWHERE.base_asset_id)

    recomputed = rebuilt(stored)
    assert recomputed == RiskContext.model_validate(row.basis["risk_context"])
    assert recomputed.exposure_usd == Decimal("36.000000000000000000")


@pytest.mark.parametrize("field", ["holdings", "cash_usd", "position_marks"])
async def test_the_stored_basis_carries_what_the_recomputation_needs(risk_db, now, trace, field):
    """Each part is actually present, rather than defaulted into existence."""
    _, sessions = risk_db
    feed, _, _ = two_pools(now)
    await hold(sessions, now, trace, market=ELSEWHERE, asset=ELSEWHERE.base_asset_id, quantity="4")
    case, _, _ = await approved_case(sessions, now, uuid4(), key="parts", feed=feed)
    service = build_fill_service(sessions, now, feed=feed)
    assert (
        await service.execute_case_fill(case.id, request_key="parts-req")
    ).kind == "paper_fill_recorded"

    async with sessions() as session:
        row = await session.scalar(
            select(TradeCaseExecutionRow).where(TradeCaseExecutionRow.trade_case_id == case.id)
        )
    assert row.basis["portfolio"][field]
    holding = row.basis["portfolio"]["holdings"][0]
    assert holding["asset_id"] == ELSEWHERE.base_asset_id
    assert holding["market_pair_id"] == ELSEWHERE.pair_id
    assert Decimal(holding["quantity"]) == Decimal("4")
    assert Decimal(holding["cost_basis_usd"]) == Decimal("100")


async def test_the_recomputation_survives_a_strict_limits_configuration(risk_db, now, trace):
    """A refusal writes no basis; this is the control that a fill still does."""
    _, sessions = risk_db
    feed, _, _ = two_pools(now)
    strict = RiskLimits(max_position_size_usd=Decimal("1"))
    case, _, _ = await approved_case(sessions, now, trace, key="strict", feed=feed)
    service = build_fill_service(sessions, now, feed=feed, limits=strict)

    result = await service.execute_case_fill(case.id, request_key="strict-req")

    assert result.reason is ExecutionRefusal.RISK_RECHECK_REFUSED
    async with sessions() as session:
        assert (await session.scalar(select(func.count()).select_from(TradeCaseExecutionRow))) == 0
