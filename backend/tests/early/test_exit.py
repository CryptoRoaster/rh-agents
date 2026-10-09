"""EARLY_PAPER_EXIT_V1, end to end: a real early entry, recorded prices, one real exit.

The entry is booked through the real early risk request and case fill, at zero
paper costs so the entry cost per unit is exactly the 1.25 reference price:
stop at 0.50, trailing armed at 2.50, a trailing exit at half the peak. Every
later price is a real observation written through the real recorder, and the
sweep reads marks and the peak from those rows — so a restart is nothing more
than a new sweep object over the same database. The sale is the existing
`PaperExitService`, judged on its own fresh ATLAS read.
"""

import asyncio
import os
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.core.clock import FixedClock
from src.core.models import RiskLimits
from src.data.tables import PositionRow, TradeCaseExitRow
from src.markets.reader import MarketReader
from src.markets.recorder import MarketRecorder
from src.orchestration.exitpolicy.early import (
    EARLY_PAPER_EXIT_V1,
    EarlyExitInputs,
    EarlyExitService,
    EarlyExitTrigger,
    ObservedPeak,
    evaluate_early,
)
from src.orchestration.exitpolicy.policy import PaperExitPolicy
from src.orchestration.exitpolicy.service import AutoExitService
from src.orchestration.paperexit.exitread import AtlasExitRead, UnavailableExitRead
from src.orchestration.reentry.models import ReentryRefusal
from src.orchestration.strategy.early import early_ledger
from tests.atlas.conftest import builder_for
from tests.early.test_fill_caps import Book
from tests.early.test_sentinel import early_ready
from tests.paperexit.conftest import build_exit_service, money
from tests.reentry.conftest import build_reentry_service
from tests.riskdata.conftest import IDENTITY, configured_costs, recorded_snapshot

HELD = IDENTITY
FRESH = timedelta(seconds=5)
ZERO = configured_costs(fee="0", slippage="0")
ENTRY = Decimal("1.25")


# ------------------------------------------------------------ the policy alone


def inputs(now, *, mark="1.25", peak=None, liquidity="750000", held=timedelta(hours=1)):
    return EarlyExitInputs(
        entry_cost_per_unit_usd=ENTRY,
        entered_at=now - held,
        mark_price_usd=None if mark is None else Decimal(mark),
        mark_observed_at=None if mark is None else now,
        peak=ObservedPeak(
            price_usd=None if peak is None else Decimal(peak), observed_at=now, observations=1
        ),
        liquidity_usd=None if liquidity is None else Decimal(liquidity),
        now=now,
    )


@pytest.mark.parametrize(
    ("overrides", "trigger", "reason"),
    [
        # −60 % exactly is a stop; a hair above is not.
        ({"mark": "0.50"}, EarlyExitTrigger.STOP_LOSS, "MARK_AT_OR_BELOW_STOP"),
        ({"mark": "0.5000001"}, None, "HOLD"),
        # Liquidity below the early entry minimum invalidates; at it, holds.
        ({"liquidity": "9999.99"}, EarlyExitTrigger.LIQUIDITY_INVALIDATION, None),
        ({"liquidity": "10000"}, None, "HOLD"),
        # Trailing not armed below 2x: a deep fall from 2.49 is not a trailing stop.
        ({"peak": "2.49", "mark": "1.20"}, None, "HOLD"),
        # Armed at exactly 2x; half the peak exactly exits, a hair above holds.
        ({"peak": "2.50", "mark": "1.25"}, EarlyExitTrigger.TRAILING_STOP, None),
        ({"peak": "2.50", "mark": "1.2500001"}, None, "HOLD_TRAILING_ACTIVE"),
        ({"peak": "4.00", "mark": "2.00"}, EarlyExitTrigger.TRAILING_STOP, None),
        # The time limit, to the second.
        ({"held": timedelta(hours=72) - timedelta(seconds=1)}, None, "HOLD"),
        ({"held": timedelta(hours=72)}, EarlyExitTrigger.TIME_EXIT, None),
    ],
)
def test_each_condition_and_its_boundary(now, overrides, trigger, reason):
    verdict = evaluate_early(EARLY_PAPER_EXIT_V1, inputs(now, **overrides))
    assert verdict.trigger is trigger
    if reason is not None:
        assert verdict.reason == reason


def test_the_mark_itself_arms_the_trailing_stop(now):
    verdict = evaluate_early(EARLY_PAPER_EXIT_V1, inputs(now, mark="2.50"))
    assert verdict.trailing_active and verdict.peak_price_usd == Decimal("2.50")
    assert verdict.trigger is None


def test_simultaneous_conditions_name_the_protective_one_and_record_all(now):
    verdict = evaluate_early(
        EARLY_PAPER_EXIT_V1,
        inputs(now, mark="0.40", liquidity="5000", held=timedelta(hours=80)),
    )
    assert verdict.trigger is EarlyExitTrigger.STOP_LOSS
    assert verdict.conditions == (
        EarlyExitTrigger.STOP_LOSS,
        EarlyExitTrigger.LIQUIDITY_INVALIDATION,
        EarlyExitTrigger.TIME_EXIT,
    )


def test_unknown_data_fires_only_what_needs_no_data(now):
    blind = inputs(now, mark=None, liquidity=None)
    assert evaluate_early(EARLY_PAPER_EXIT_V1, blind).reason == "HOLD_MARK_UNKNOWN"
    late = inputs(now, mark=None, liquidity=None, held=timedelta(hours=72))
    assert evaluate_early(EARLY_PAPER_EXIT_V1, late).trigger is EarlyExitTrigger.TIME_EXIT


def test_the_contract_numbers():
    policy = EARLY_PAPER_EXIT_V1
    assert policy.version == "EARLY_PAPER_EXIT_V1"
    assert policy.stop_loss_bps == 6000
    assert policy.trailing_activation_multiple == Decimal(2)
    assert policy.trailing_drawdown_bps == 5000
    assert policy.max_holding_seconds == 72 * 3600
    assert policy.min_liquidity_usd == Decimal(10_000)


# ------------------------------------------------------- the real exit path


async def early_entry(sessions, now):
    """An early case on the held market, through the real risk request and fill."""
    book = Book(sessions, now, costs=ZERO)
    trade_case = await early_ready(book.risk, sessions, now, uuid4(), key="early-held")
    approval = await book.risk.request_risk_evaluation(trade_case.id, request_key="early-held")
    assert approval.kind == "risk_request_evaluated", approval
    filled = await book.filler().execute_case_fill(trade_case.id, request_key="early-held")
    assert filled.kind == "paper_fill_recorded", getattr(filled, "detail", None)
    return trade_case


async def observe(sessions, at, *, price, liquidity=None, label=None):
    """One real observation of the held market, written through the recorder."""
    extra = {} if liquidity is None else {"liquidity": Decimal(liquidity)}
    snapshot = recorded_snapshot(
        at,
        age=FRESH,
        metadata_age=FRESH,
        pair_id=HELD.pair_id,
        base_asset_id=HELD.base_asset_id,
        label=label or f"exit-{at.isoformat()}-{price}",
        price=Decimal(price),
        **extra,
    )
    await MarketRecorder(sessions, clock=FixedClock(at)).record(snapshot)
    return snapshot


def sweeper(sessions, at, *, read="fresh", **overrides):
    clock = FixedClock(at)
    markets = MarketReader(sessions, clock=clock)
    exit_read = AtlasExitRead(builder=builder_for(at), clock=clock) if read == "fresh" else read
    return EarlyExitService(
        sessions=sessions,
        exits=build_exit_service(sessions, at, feed=markets, exit_read=exit_read, costs=ZERO),
        markets=markets,
        limits=RiskLimits(),
        clock=clock,
        **overrides,
    )


async def exit_rows(sessions):
    async with sessions() as session:
        return (await session.scalars(select(TradeCaseExitRow))).all()


async def held_quantity(sessions):
    async with sessions() as session:
        return await session.scalar(
            select(PositionRow.quantity).where(PositionRow.asset_id == HELD.base_asset_id)
        )


async def swept(sessions, at, **kwargs):
    return await sweeper(sessions, at, **kwargs).sweep()


async def test_a_stop_at_minus_sixty_sells_everything_once(risk_db, now):
    _, sessions = risk_db
    await early_entry(sessions, now)
    at = now + timedelta(minutes=10)
    await observe(sessions, at, price="0.50")

    result = await swept(sessions, at)

    assert (result.evaluated, result.triggered, result.executed) == (1, 1, 1), result
    [row] = await exit_rows(sessions)
    assert (row.exit_trigger, row.exit_policy_version) == ("STOP_LOSS", "EARLY_PAPER_EXIT_V1")
    assert row.basis["exit_basis"] == "FRESH_EXIT_READ"
    # The whole holding: eight tokens bought for ten dollars, eight sold.
    assert money(row.quantity) == money(8)
    assert money(await held_quantity(sessions)) == money(0)
    # Realised in the early book: 10.00 in, 4.00 out.
    assert money(row.realized_pnl_usd) == money(-6)
    async with sessions() as session:
        book = (await early_ledger(session)).at(at)
    assert (book.open_positions, money(book.realized_loss_today_usd)) == (0, money(6))


async def test_above_the_stop_nothing_is_sold(risk_db, now):
    _, sessions = risk_db
    await early_entry(sessions, now)
    at = now + timedelta(minutes=10)
    await observe(sessions, at, price="0.51")

    result = await swept(sessions, at)

    assert (result.evaluated, result.held, result.executed) == (1, 1, 0)
    assert await exit_rows(sessions) == []


async def test_the_peak_survives_a_restart_and_arms_the_trailing_stop(risk_db, now):
    _, sessions = risk_db
    await early_entry(sessions, now)
    t1 = now + timedelta(hours=1)
    peak = await observe(sessions, t1, price="3.00")
    # One sweep sees the peak and holds: armed, nowhere near half of it.
    first = await swept(sessions, t1)
    assert (first.held, first.executed) == (1, 0)

    # A new process later: nothing carried over except the database.
    t2 = now + timedelta(hours=5)
    await observe(sessions, t2, price="1.60")
    second = await swept(sessions, t2)
    assert (second.held, second.executed) == (1, 0)  # 1.60 > 1.50: still above
    t3 = now + timedelta(hours=6)
    await observe(sessions, t3, price="1.50")
    third = await swept(sessions, t3)

    assert third.triggers == {"TRAILING_STOP": 1} and third.executed == 1
    [row] = await exit_rows(sessions)
    verdict = row.exit_trigger_basis["verdict"]
    assert verdict["trailing_active"] is True
    assert Decimal(verdict["peak_price_usd"]) == Decimal("3.00")
    recorded_peak = row.exit_trigger_basis["inputs"]["peak"]
    assert recorded_peak["observed_at"].startswith(peak.observed_at.isoformat()[:19])
    assert recorded_peak["observation_id"] is not None
    assert peak.price.value_usd == Decimal("3.00")
    # Sold above entry: a profit, not a loss, in the early book.
    assert row.realized_pnl_usd > 0


async def test_below_two_x_a_deep_fall_is_not_a_trailing_stop(risk_db, now):
    _, sessions = risk_db
    await early_entry(sessions, now)
    await observe(sessions, now + timedelta(hours=1), price="2.49")
    at = now + timedelta(hours=2)
    await observe(sessions, at, price="1.20")

    result = await swept(sessions, at)

    assert (result.held, result.executed) == (1, 0)


async def test_the_seventy_two_hour_limit(risk_db, now):
    _, sessions = risk_db
    await early_entry(sessions, now)
    before = now + timedelta(hours=72) - timedelta(seconds=1)
    await observe(sessions, before, price="1.30")
    assert (await swept(sessions, before)).executed == 0

    at = now + timedelta(hours=72)
    await observe(sessions, at, price="1.30")
    result = await swept(sessions, at)

    assert result.triggers == {"TIME_EXIT": 1} and result.executed == 1


async def test_lost_liquidity_invalidates_the_position(risk_db, now):
    _, sessions = risk_db
    await early_entry(sessions, now)
    at = now + timedelta(minutes=30)
    await observe(sessions, at, price="1.30", liquidity="9999")

    result = await swept(sessions, at)

    assert result.triggers == {"LIQUIDITY_INVALIDATION": 1} and result.executed == 1


async def test_simultaneous_breaches_are_one_exit_named_by_the_stop(risk_db, now):
    _, sessions = risk_db
    await early_entry(sessions, now)
    at = now + timedelta(hours=73)
    await observe(sessions, at, price="0.40", liquidity="5000")

    result = await swept(sessions, at)

    assert result.triggers == {"STOP_LOSS": 1} and result.executed == 1
    [row] = await exit_rows(sessions)
    assert row.exit_trigger_basis["verdict"]["conditions"] == [
        "STOP_LOSS",
        "LIQUIDITY_INVALIDATION",
        "TIME_EXIT",
    ]


async def test_a_stale_mark_sells_nothing_and_says_so(risk_db, now):
    _, sessions = risk_db
    await early_entry(sessions, now)
    # A crash, recorded long ago: too old to be a mark now.
    await observe(sessions, now + timedelta(minutes=1), price="0.10")
    at = now + timedelta(hours=1)

    result = await swept(sessions, at)

    assert (result.held, result.executed) == (1, 0)
    assert result.refusals == {"EARLY_EXIT_MARK_UNKNOWN": 1}
    assert await exit_rows(sessions) == []


async def test_a_time_exit_without_a_price_is_not_booked_until_one_exists(risk_db, now):
    _, sessions = risk_db
    await early_entry(sessions, now)
    at = now + timedelta(hours=72)

    blocked = await swept(sessions, at)

    assert blocked.triggers == {"TIME_EXIT": 1} and blocked.executed == 0
    assert blocked.refusals, blocked
    assert await exit_rows(sessions) == []
    assert money(await held_quantity(sessions)) == money(8)

    later = at + timedelta(minutes=5)
    await observe(sessions, later, price="1.30")
    done = await swept(sessions, later)
    assert done.executed == 1
    assert len(await exit_rows(sessions)) == 1


async def test_a_failed_exit_is_retried_and_books_once(risk_db, now):
    _, sessions = risk_db
    await early_entry(sessions, now)
    at = now + timedelta(minutes=10)
    await observe(sessions, at, price="0.45")

    failed = await swept(sessions, at, read=UnavailableExitRead("EXIT_ONCHAIN_SOURCE_UNAVAILABLE"))
    assert failed.executed == 0 and failed.refusals, failed
    assert await exit_rows(sessions) == []

    retry = at + timedelta(minutes=2)
    await observe(sessions, retry, price="0.45")
    assert (await swept(sessions, retry)).executed == 1
    # And nothing more, however often it is asked again.
    again = retry + timedelta(minutes=2)
    await observe(sessions, again, price="0.45")
    repeat = await swept(sessions, again)
    assert (repeat.evaluated, repeat.executed) == (0, 0)
    assert len(await exit_rows(sessions)) == 1


async def test_a_closed_early_cycle_is_never_re_entered(risk_db, now):
    _, sessions = risk_db
    await early_entry(sessions, now)
    at = now + timedelta(minutes=10)
    await observe(sessions, at, price="0.50")
    await swept(sessions, at)
    [row] = await exit_rows(sessions)

    result = await build_reentry_service(sessions, at).open_reentry(
        row.exit_id, request_key="again"
    )

    assert result.kind == "reentry_refused"
    assert result.reason is ReentryRefusal.STRATEGY_REENTRY_NOT_PERMITTED


async def test_the_normal_policy_never_closes_an_early_position(risk_db, now):
    _, sessions = risk_db
    await early_entry(sessions, now)
    at = now + timedelta(hours=10)
    await observe(sessions, at, price="0.10")
    clock = FixedClock(at)
    markets = MarketReader(sessions, clock=clock)
    normal = AutoExitService(
        sessions=sessions,
        exits=build_exit_service(
            sessions,
            at,
            feed=markets,
            exit_read=AtlasExitRead(builder=builder_for(at), clock=clock),
        ),
        markets=markets,
        # A policy that would close it three times over.
        policy=PaperExitPolicy(stop_loss_bps=100, take_profit_bps=100, max_holding_seconds=60),
        limits=RiskLimits(),
        clock=clock,
    )

    result = await normal.sweep()

    assert (result.evaluated, result.executed) == (0, 0)
    assert result.refusals == {"EARLY_POSITION_OWN_EXIT_POLICY": 1}
    assert await exit_rows(sessions) == []


async def test_the_early_policy_never_closes_a_normal_position(risk_db, now, trace):
    from tests.paperexit.conftest import entered, market_feed

    _, sessions = risk_db
    await entered(sessions, now, trace, feed=market_feed(now))
    at = now + timedelta(hours=80)

    result = await swept(sessions, at)

    assert (result.evaluated, result.triggered, result.executed) == (0, 0, 0)
    assert await exit_rows(sessions) == []


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required")
async def test_racing_sweeps_sell_one_holding_once(risk_db, now):
    _, sessions = risk_db
    await early_entry(sessions, now)
    at = now + timedelta(minutes=10)
    await observe(sessions, at, price="0.40")

    results = await asyncio.gather(*(swept(sessions, at) for _ in range(3)))

    assert sum(item.executed for item in results) == 1
    rows = await exit_rows(sessions)
    assert len(rows) == 1 and rows[0].exit_trigger == "STOP_LOSS"
    assert money(await held_quantity(sessions)) == money(0)
    async with sessions() as session:
        book = (await early_ledger(session)).at(at)
    # Eight tokens bought for 10.00, sold at 0.40: 3.20 back, 6.80 lost — once.
    assert money(book.realized_loss_today_usd) == money("6.8")


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required")
async def test_racing_sweeps_at_different_minutes_still_sell_once(risk_db, now):
    """Different request keys, one cycle: the database allows one exit."""
    _, sessions = risk_db
    await early_entry(sessions, now)
    first = now + timedelta(minutes=10)
    second = first + timedelta(minutes=1)
    await observe(sessions, first, price="0.40")
    await observe(sessions, second, price="0.40", label="exit-second")

    results = await asyncio.gather(swept(sessions, first), swept(sessions, second))

    assert sum(item.executed for item in results) == 1
    assert len(await exit_rows(sessions)) == 1


# ------------------------------------------------ peak completeness (> 5000)


def _with_provider(value, old, new):
    """The same observation, as another provider would have recorded it."""
    if isinstance(value, dict):
        return {
            key: (new if key == "provider" and item == old else _with_provider(item, old, new))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_with_provider(item, old, new) for item in value]
    return value


def _rows(snapshots, *, provider=None, fixture=None):
    """Observation rows exactly as the recorder writes them, inserted in bulk."""
    rows = []
    for snapshot in snapshots:
        payload = snapshot.model_dump(mode="json")
        if snapshot.pair.pool_locator is None:
            payload["pair"].pop("pool_locator", None)
        if provider is not None:
            payload = _with_provider(payload, snapshot.provider, provider)
        if fixture is not None:
            payload["is_fixture"] = fixture
        rows.append(
            {
                "id": uuid4(),
                "schema_version": snapshot.schema_version,
                "provider": provider or snapshot.provider,
                "chain": snapshot.chain,
                "network": snapshot.network,
                "asset_id": snapshot.asset_id,
                "pair_id": snapshot.pair.pair_id,
                "correlation_id": snapshot.correlation_id,
                "observed_at": snapshot.observed_at,
                "recorded_at": snapshot.observed_at,
                "freshness_at": snapshot.freshness_at,
                "available": snapshot.available,
                "is_fixture": snapshot.is_fixture if fixture is None else fixture,
                "payload": payload,
            }
        )
    return rows


async def bulk_observe(sessions, rows):
    from sqlalchemy import insert

    from src.data.tables import MarketObservationRow

    async with sessions.begin() as session:
        for start in range(0, len(rows), 1000):
            await session.execute(insert(MarketObservationRow), rows[start : start + 1000])


def held_at(at, price, *, pair=None, label):
    identity = pair or HELD
    return recorded_snapshot(
        at,
        age=timedelta(0),
        metadata_age=timedelta(0),
        pair_id=identity.pair_id,
        base_asset_id=identity.base_asset_id,
        label=label,
        price=Decimal(price),
    )


async def test_a_peak_older_than_five_thousand_observations_still_arms_the_trailing_stop(
    risk_db, now
):
    """Entry 1.00, peak 3.00, then 5,001 lower readings, now 1.40 under the 1.50 level."""
    from tests.early.test_fill_caps import MARKETS
    from tests.riskrequest.conftest import fresh_snapshot

    _, sessions = risk_db
    book = Book(sessions, now, costs=ZERO)
    book.feed.replace(fresh_snapshot(now, price=Decimal("1.00")))
    trade_case = await early_ready(book.risk, sessions, now, uuid4(), key="early-peak")
    assert (await book.risk.request_risk_evaluation(trade_case.id, request_key="early-peak")).kind
    filled = await book.filler().execute_case_fill(trade_case.id, request_key="early-peak")
    assert filled.kind == "paper_fill_recorded", getattr(filled, "detail", None)

    peak_at = now + timedelta(minutes=1)
    peak = held_at(peak_at, "3.00", label="true-peak")
    later = [
        held_at(now + timedelta(minutes=2, seconds=index), "1.60", label=f"lower-{index}")
        for index in range(5001)
    ]
    # Higher prices that are not this market's: another pool, another provider,
    # and a fixture of this very pool. None of them may be the peak.
    other_pool = held_at(peak_at, "10.00", pair=MARKETS[1], label="other-pool")
    other_provider = held_at(peak_at, "10.00", label="other-provider")
    fixture = held_at(peak_at, "10.00", label="fixture-copy")
    await bulk_observe(
        sessions,
        _rows([peak, *later, other_pool])
        + _rows([other_provider], provider="another-provider")
        + _rows([fixture], fixture=True),
    )
    at = now + timedelta(hours=3)
    await observe(sessions, at, price="1.40")

    result = await swept(sessions, at)

    assert result.triggers == {"TRAILING_STOP": 1} and result.executed == 1, result
    [row] = await exit_rows(sessions)
    recorded = row.exit_trigger_basis["inputs"]["peak"]
    assert Decimal(recorded["price_usd"]) == Decimal("3.00")
    assert recorded["observation_id"] is not None
    assert recorded["observed_at"].startswith(peak.observed_at.isoformat()[:19])
    assert recorded["truncated"] is False


def _corrupt(row):
    """A stored row claiming an available price whose payload is not a snapshot."""
    broken = dict(row)
    payload = dict(row["payload"])
    payload.pop("pair")
    broken["payload"] = payload
    return broken


async def test_a_corrupt_higher_row_is_skipped_and_the_valid_peak_decides(risk_db, now):
    _, sessions = risk_db
    await early_entry(sessions, now)
    t1 = now + timedelta(minutes=1)
    [corrupt] = _rows([held_at(t1, "9.00", label="corrupt")])
    await bulk_observe(sessions, [_corrupt(corrupt), *_rows([held_at(t1, "3.00", label="valid")])])
    at = now + timedelta(hours=1)
    await observe(sessions, at, price="1.50")

    result = await swept(sessions, at)

    # A trailing exit on a lower-bound peak is still correct: the true peak can
    # only be higher, so its trailing level can only be higher too.
    assert result.triggers == {"TRAILING_STOP": 1}, result
    [row] = await exit_rows(sessions)
    peak = row.exit_trigger_basis["inputs"]["peak"]
    assert Decimal(peak["price_usd"]) == Decimal("3.00")
    assert peak["truncated"] is True


async def test_a_peak_that_cannot_be_read_is_reported_incomplete(risk_db, now):
    _, sessions = risk_db
    await early_entry(sessions, now)
    t1 = now + timedelta(minutes=1)
    await bulk_observe(
        sessions,
        [_corrupt(item) for item in _rows([held_at(t1, "9.00", label="corrupt-only")])],
    )
    at = now + timedelta(hours=1)
    await observe(sessions, at, price="1.30")

    result = await swept(sessions, at)

    # The unreadable row is skipped, never invented into a peak, and the
    # incompleteness is visible rather than read as "no peak".
    assert (result.held, result.executed) == (1, 0)
    assert result.refusals == {"EARLY_EXIT_PEAK_INCOMPLETE": 1}


async def test_liquidity_from_another_provider_never_invalidates_the_position(risk_db, now):
    _, sessions = risk_db
    await early_entry(sessions, now)
    at = now + timedelta(minutes=30)
    await observe(sessions, at - timedelta(seconds=10), price="1.30")
    # Newer, same pool id, a different provider — and thin. Not this market's reading.
    foreign = recorded_snapshot(
        at,
        age=FRESH,
        metadata_age=FRESH,
        pair_id=HELD.pair_id,
        base_asset_id=HELD.base_asset_id,
        label="foreign-provider",
        price=Decimal("1.30"),
        liquidity=Decimal("5000"),
    )
    await bulk_observe(sessions, _rows([foreign], provider="another-provider"))

    result = await swept(sessions, at)

    assert result.triggers.get("LIQUIDITY_INVALIDATION") is None, result
    assert result.executed == 0


def _priced(row, value):
    """A stored row whose recorded price field is `value` (or absent for ...)."""
    broken = dict(row)
    payload = dict(row["payload"])
    price = dict(payload["price"])
    if value is ...:
        price.pop("value_usd", None)
    else:
        price["value_usd"] = value
    payload["price"] = price
    broken["payload"] = payload
    return broken


@pytest.mark.parametrize(
    "value",
    ["unbekannt", "", ..., "1e40", "9" * 30, "-5", "NaN", None],
    ids=["text", "empty", "missing", "exponent-huge", "huge", "negative", "nan", "null"],
)
async def test_a_damaged_price_never_breaks_or_becomes_the_peak(risk_db, now, value):
    _, sessions = risk_db
    await early_entry(sessions, now)
    t1 = now + timedelta(minutes=1)
    [damaged] = _rows([held_at(t1, "9.00", label=f"damaged-{value!r}")])
    await bulk_observe(
        sessions, [_priced(damaged, value), *_rows([held_at(t1, "3.00", label="valid-peak")])]
    )
    at = now + timedelta(hours=1)
    await observe(sessions, at, price="1.50")

    result = await swept(sessions, at)

    # The valid 3.00 decides: 1.50 is exactly half of it.
    assert result.triggers == {"TRAILING_STOP": 1}, result
    [row] = await exit_rows(sessions)
    peak = row.exit_trigger_basis["inputs"]["peak"]
    assert Decimal(peak["price_usd"]) == Decimal("3.00")
    # A damaged row in the window is never silently a complete history.
    assert peak["truncated"] is True


async def test_tiny_prices_in_exponent_form_are_ranked_not_called_damaged(risk_db, now):
    """Meme prices are stored as e.g. '4.5E-7'; they are prices, and complete ones."""
    from src.orchestration.exitpolicy.early import observed_peak

    _, sessions = risk_db
    await early_entry(sessions, now)
    t1 = now + timedelta(minutes=1)
    tiny = [
        held_at(t1 + timedelta(seconds=index), price, label=f"tiny-{index}")
        for index, price in enumerate(["0.0000001", "0.00000045", "0.000000012345"])
    ]
    rows = _rows(tiny)
    assert {row["payload"]["price"]["value_usd"] for row in rows} == {"1E-7", "4.5E-7", "1.2345E-8"}
    await bulk_observe(sessions, rows)
    async with sessions() as session:
        position = await session.scalar(
            select(PositionRow).where(PositionRow.asset_id == HELD.base_asset_id)
        )
        peak = await observed_peak(
            session, position_from(position), since=now, until=now + timedelta(hours=1)
        )

    assert peak.price_usd == Decimal("4.5E-7")
    assert peak.observations == 3
    assert peak.truncated is False


def position_from(row):
    from src.data.repository import read_position

    return read_position(row)
