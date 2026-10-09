"""Every open position's market is observed again first, by its full identity.

The stage under test is the real `BoundedMarketAcquisition` over the real
transport, adapter, normalization and recorder; the only substitute is the
provider's HTTP response. The positions are real ones, booked through the real
early and normal entry paths, so each holding's market identity is the case's
own — base and quote asset, venue and pool locator included.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.core.clock import FixedClock
from src.data.tables import MarketObservationRow
from src.markets.reader import MarketReader
from src.runner.acquisition import BoundedMarketAcquisition
from src.runner.models import AcquisitionLimits, AcquisitionNeed, AcquisitionOutcome
from tests.atlas.conftest import QUOTE
from tests.early.test_fill_caps import MARKETS, Book
from tests.riskdata.conftest import RunningSystem, configured_costs, market_for
from tests.runner.conftest import runner_settings
from tests.runner.provider import MarketProvider, pool

ZERO = configured_costs(fee="0", slippage="0")
NORMAL = tuple(
    market_for(token=f"{index + 0x91:02x}" * 20, pool=f"{index + 0xA1:02x}" * 20)
    for index in range(4)
)


class Deadline:
    remaining = 60.0
    expired = False


def address(identity, part):
    """The pool or token address inside a market identity."""
    if part == "pool":
        return identity.pair_id.rsplit(":", 1)[-1]
    return identity.base_asset_id.rsplit(":", 1)[-1]


def answer(identity, *, price="1.10", liquidity="750000", venue="uniswap-v3"):
    """The provider's own reading of one market, as `pools/multi` returns it."""
    return pool(
        address(identity, "pool"),
        base=address(identity, "token"),
        quote=QUOTE[2:] and QUOTE,
        price=price,
        liquidity=liquidity,
        quote_price="1",
        venue=venue,
    )


def stage(sessions, at, provider, **limits):
    values = {
        "max_markets": 4,
        "max_discovery_requests": 0,
        "max_provider_requests": 6,
        "max_http_attempts": 8,
        "max_seconds": 30,
    }
    values.update(limits)
    settings = runner_settings(
        market_provider="geckoterminal",
        market_chains="robinhood",
        paper_runner_market_acquisition_enabled=True,
        geckoterminal_retry_delay_seconds=0,
    )
    clock = FixedClock(at)
    return BoundedMarketAcquisition(
        settings,
        sessions,
        MarketReader(sessions, clock=clock),
        AcquisitionLimits(**values),
        pause=RunningSystem(),
        clock=clock,
        http=provider.transport(),
    )


async def portfolio(sessions, now, *, early=0, normal=0):
    """Real early and normal holdings, entered through their own real paths.

    One shared market feed, so that every later entry can value the holdings
    already booked — the risk request refuses a portfolio it cannot mark.
    """
    from tests.paperexit.conftest import entered
    from tests.riskrequest.conftest import fresh_snapshot

    book = Book(sessions, now, costs=ZERO)
    for index, item in enumerate(NORMAL[:normal]):
        book.feed.replace(
            fresh_snapshot(
                now,
                pair_id=item.pair_id,
                base_asset_id=item.base_asset_id,
                label=f"normal-entry-{index}",
            )
        )
    for index in range(early):
        await book.entered(index)
    for index, identity in enumerate(NORMAL[:normal]):
        await entered(
            sessions, now, uuid4(), key=f"normal-{index}", feed=book.feed, identity=identity
        )
    # Every entry was decided on a recorded observation, as in production:
    # write the ones this portfolio was entered on through the real recorder.
    from src.markets.recorder import MarketRecorder

    recorder = MarketRecorder(sessions, clock=FixedClock(now))
    for snapshot in book.feed._by_pair.values():
        await recorder.record(snapshot)
    return (*MARKETS[:early], *NORMAL[:normal])


async def early_positions(sessions, now, count):
    return await portfolio(sessions, now, early=count)


def asked_pools(provider):
    return [item.rsplit("/", 1)[-1].split(",") for item in provider.multi_requests]


def positions(summary):
    return [
        item for item in summary.markets if item.need == AcquisitionNeed.POSITION_VALUATION.value
    ]


async def observed(sessions, identity):
    async with sessions() as session:
        return (
            await session.scalars(
                select(MarketObservationRow).where(MarketObservationRow.pair_id == identity.pair_id)
            )
        ).all()


# ------------------------------------------------------------------ coverage


async def test_one_early_position_is_observed_again(risk_db, now):
    _, sessions = risk_db
    (held,) = await early_positions(sessions, now, 1)
    at = now + timedelta(minutes=5)
    provider = MarketProvider(targeted=[answer(held)])

    summary = await stage(sessions, at, provider).execute(Deadline())

    assert [(item.pair_id, item.outcome) for item in positions(summary)] == [
        (held.pair_id, AcquisitionOutcome.RECORDED.value)
    ]
    coverage = summary.positions
    assert (coverage.open_positions, coverage.markets, coverage.answered) == (1, 1, 1)
    assert coverage.complete is True


async def test_five_early_positions_are_all_observed_despite_a_market_budget_of_four(risk_db, now):
    _, sessions = risk_db
    held = await early_positions(sessions, now, 5)
    at = now + timedelta(minutes=5)
    provider = MarketProvider(targeted=[answer(item) for item in held])

    summary = await stage(sessions, at, provider, max_markets=4).execute(Deadline())

    # One request for every holding: none crowded out by the case budget.
    assert sorted(asked_pools(provider)[0]) == sorted(address(item, "pool") for item in held)
    assert {item.pair_id for item in positions(summary)} == {item.pair_id for item in held}
    assert all(item.outcome == AcquisitionOutcome.RECORDED.value for item in positions(summary))
    coverage = summary.positions
    assert (coverage.open_positions, coverage.markets, coverage.answered) == (5, 5, 5)
    assert coverage.complete is True
    for item in held:
        # The entry's own observation, and this pass's new one.
        assert len(await observed(sessions, item)) == 2


async def test_four_normal_and_five_early_positions_are_all_observed(risk_db, now):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=5, normal=4)
    at = now + timedelta(minutes=5)
    provider = MarketProvider(targeted=[answer(item) for item in held])

    summary = await stage(sessions, at, provider).execute(Deadline())

    coverage = summary.positions
    assert (coverage.open_positions, coverage.markets, coverage.answered) == (9, 9, 9)
    assert coverage.complete is True
    assert len(provider.multi_requests) == 1  # nine pools, one bounded request


async def test_a_position_capacity_below_the_open_markets_is_a_visible_deficit(risk_db, now):
    _, sessions = risk_db
    held = await early_positions(sessions, now, 5)
    at = now + timedelta(minutes=5)
    provider = MarketProvider(targeted=[answer(item) for item in held])

    summary = await stage(sessions, at, provider, max_position_markets=3).execute(Deadline())

    coverage = summary.positions
    assert (coverage.markets, coverage.asked, coverage.answered) == (5, 3, 3)
    assert coverage.not_attempted == 2 and coverage.complete is False
    left = [
        item
        for item in positions(summary)
        if item.outcome == AcquisitionOutcome.NOT_ATTEMPTED.value
    ]
    assert [item.reason for item in left] == ["POSITION_CAPACITY_EXCEEDED"] * 2
    # Deterministic: the same holdings are left out every time.
    again = await stage(
        sessions,
        at + timedelta(seconds=1),
        MarketProvider(targeted=[answer(item) for item in held]),
        max_position_markets=3,
    ).execute(Deadline())
    assert sorted(i.pair_id for i in positions(again) if i.reason) == sorted(
        i.pair_id for i in left
    )


async def test_discovery_never_takes_a_position_slot(risk_db, now):
    _, sessions = risk_db
    held = await early_positions(sessions, now, 5)
    at = now + timedelta(minutes=5)
    fresh_pools = [
        answer(market_for(token=f"{i + 0x11:02x}" * 20, pool=f"{i + 0x21:02x}" * 20))
        for i in range(6)
    ]
    provider = MarketProvider(discovery=fresh_pools, targeted=[answer(item) for item in held])

    summary = await stage(sessions, at, provider, max_markets=2, max_discovery_requests=1).execute(
        Deadline()
    )

    assert summary.positions.complete is True
    assert summary.positions.answered == 5
    discovered = [i for i in summary.markets if i.need == AcquisitionNeed.NEW_CANDIDATE.value]
    assert len(discovered) <= 2


# ------------------------------------------------------- identity and data


async def test_a_reading_naming_another_market_under_the_same_pool_is_refused(risk_db, now):
    """Same pool id, another venue: not this holding's market, never stored as it."""
    _, sessions = risk_db
    (held,) = await early_positions(sessions, now, 1)
    at = now + timedelta(minutes=5)
    provider = MarketProvider(targeted=[answer(held, venue="another-venue")])

    summary = await stage(sessions, at, provider).execute(Deadline())

    [entry] = positions(summary)
    assert (entry.outcome, entry.reason) == (
        AcquisitionOutcome.REFUSED.value,
        "MARKET_IDENTITY_MISMATCH",
    )
    assert summary.positions.complete is False
    # Only the entry's own observation: the other market's answer was not stored.
    assert len(await observed(sessions, held)) == 1


async def test_a_market_the_provider_does_not_return_does_not_stop_the_others(risk_db, now):
    _, sessions = risk_db
    held = await early_positions(sessions, now, 3)
    at = now + timedelta(minutes=5)
    # The second market is missing from the answer; the first and third are there.
    provider = MarketProvider(targeted=[answer(held[0]), answer(held[2])])

    summary = await stage(sessions, at, provider).execute(Deadline())

    outcomes = {item.pair_id: (item.outcome, item.reason) for item in positions(summary)}
    assert outcomes[held[1].pair_id] == (AcquisitionOutcome.REFUSED.value, "MARKET_NOT_RETURNED")
    assert outcomes[held[0].pair_id][0] == AcquisitionOutcome.RECORDED.value
    assert outcomes[held[2].pair_id][0] == AcquisitionOutcome.RECORDED.value
    assert (summary.positions.answered, summary.positions.complete) == (2, False)


async def test_a_failing_provider_is_counted_against_coverage(risk_db, now):
    _, sessions = risk_db
    held = await early_positions(sessions, now, 2)
    at = now + timedelta(minutes=5)
    provider = MarketProvider(targeted=[answer(item) for item in held], status=503)

    summary = await stage(sessions, at, provider).execute(Deadline())

    coverage = summary.positions
    assert (coverage.asked, coverage.answered, coverage.failed) == (2, 0, 2)
    assert coverage.complete is False


async def test_an_unpriced_answer_is_recorded_as_unavailable_never_as_fresh(risk_db, now):
    _, sessions = risk_db
    (held,) = await early_positions(sessions, now, 1)
    at = now + timedelta(minutes=5)
    provider = MarketProvider(targeted=[answer(held, price=None)])

    summary = await stage(sessions, at, provider).execute(Deadline())

    # Answered and stored as it is — but not a usable mark for anyone.
    assert summary.positions.answered == 1
    reader = MarketReader(sessions, clock=FixedClock(at))
    from src.markets.scope import MarketScope

    assert await reader.latest_in(MarketScope.of(held)) is None or (
        (await reader.latest_in(MarketScope.of(held))).price.value_usd is None
    )


# ---------------------------------------------------------------- exits


async def test_the_exit_sweep_marks_from_the_observation_this_run_recorded(risk_db, now):
    """Acquisition first, then the early sweep, on the held market's own reading."""
    from tests.early.test_exit import sweeper

    _, sessions = risk_db
    from tests.early.test_exit import early_entry

    await early_entry(sessions, now)
    from src.markets.recorder import MarketRecorder
    from tests.riskdata.conftest import IDENTITY
    from tests.riskrequest.conftest import fresh_snapshot

    # The observation the entry was decided on, recorded as in production.
    await MarketRecorder(sessions, clock=FixedClock(now)).record(
        fresh_snapshot(now, price=Decimal("1.00"), label="entry-reading")
    )

    at = now + timedelta(minutes=10)
    provider = MarketProvider(targeted=[answer(IDENTITY, price="0.40")])
    summary = await stage(sessions, at, provider).execute(Deadline())
    assert summary.positions.answered == 1

    result = await sweeper(sessions, at).sweep()

    assert result.triggers == {"STOP_LOSS": 1} and result.executed == 1, result


@pytest.mark.parametrize("count", [0])
async def test_no_positions_means_complete_coverage_of_nothing(risk_db, now, count):
    _, sessions = risk_db
    summary = await stage(sessions, now, MarketProvider()).execute(Deadline())
    assert summary.positions.open_positions == 0 and summary.positions.complete is True
    assert Decimal(0) == count


async def test_a_newer_recording_of_another_market_under_the_pool_does_not_redirect(risk_db, now):
    """The target is the case's market, not whatever was recorded last for the pool.

    The case is opened with a full identity, locator included, as COMMANDER
    opens one from a version-3 observation.
    """
    from src.markets.models import MarketSnapshot
    from src.markets.recorder import MarketRecorder
    from tests.early.test_sentinel import early_ready
    from tests.riskrequest.conftest import fresh_snapshot

    _, sessions = risk_db
    market = MARKETS[0]
    entry_reading = fresh_snapshot(
        now, pair_id=market.pair_id, base_asset_id=market.base_asset_id, label="entry"
    )
    held = entry_reading.pair.market_identity
    assert held.pool_locator is not None
    book = Book(sessions, now, costs=ZERO)
    book.feed.replace(entry_reading)
    trade_case = await early_ready(book.risk, sessions, now, uuid4(), key="located", identity=held)
    await book.risk.request_risk_evaluation(trade_case.id, request_key="located")
    filled = await book.filler().execute_case_fill(trade_case.id, request_key="located")
    assert filled.kind == "paper_fill_recorded", getattr(filled, "detail", None)

    # A newer recorded reading of the same pool id, naming another venue.
    other = fresh_snapshot(
        now + timedelta(minutes=1),
        pair_id=market.pair_id,
        base_asset_id=market.base_asset_id,
        label="other-venue",
    )

    def swap(value):
        if isinstance(value, dict):
            return {key: swap(item) for key, item in value.items()}
        if isinstance(value, list):
            return [swap(item) for item in value]
        return "another-venue" if value == "uniswap-v3" else value

    await MarketRecorder(sessions, clock=FixedClock(now + timedelta(minutes=1))).record(
        MarketSnapshot.model_validate(swap(other.model_dump(mode="json")))
    )
    at = now + timedelta(minutes=5)
    provider = MarketProvider(targeted=[answer(market)])

    summary = await stage(sessions, at, provider).execute(Deadline())

    [entry] = positions(summary)
    assert entry.outcome == AcquisitionOutcome.RECORDED.value, summary
    assert summary.positions.complete is True
