"""PAPER exits read the held market's own stream, whatever else observed the pool.

Every reading here is a real row in `market_observations`, read back through
the real `MarketReader`. The held market is the case's: provider
`geckoterminal`, its chain, network, pool and assets. Other sources of the same
pool are recorded beside it — newer, thinner, cheaper — and must never be what
an exit is decided or executed on, while the held market's own fresh reading
must always be found.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select

from src.core.clock import FixedClock
from src.core.models import RiskLimits
from src.data.tables import MarketObservationRow, PositionRow, TradeCaseExitRow
from src.markets.models import MarketSnapshot
from src.markets.reader import MarketReader
from src.markets.recorder import MarketRecorder
from src.markets.scope import MarketScope, describes_market
from src.orchestration.exitpolicy.policy import PaperExitPolicy
from src.orchestration.exitpolicy.service import AutoExitService
from src.orchestration.paperexit.exitread import AtlasExitRead
from tests.atlas.conftest import builder_for
from tests.paperexit.conftest import build_exit_service, entered, recorded_snapshot
from tests.paperexit.test_market_identity import foreign
from tests.riskdata.conftest import IDENTITY

FRESH = timedelta(seconds=5)
LATER = timedelta(minutes=10)
POLICY = PaperExitPolicy(stop_loss_bps=2000, take_profit_bps=5000, max_holding_seconds=6 * 3600)


def held(at, *, price="1.25", liquidity=None, age=FRESH, label=None):
    extra = {} if liquidity is None else {"liquidity": Decimal(liquidity)}
    return recorded_snapshot(
        at,
        age=age,
        metadata_age=age,
        price=Decimal(price),
        label=label or f"own-{at.isoformat()}-{price}-{liquidity}",
        **extra,
    )


async def record(sessions, *snapshots):
    """Write readings as the recorder stores them, whatever source they state."""
    rows = []
    for snapshot in snapshots:
        payload = snapshot.model_dump(mode="json")
        if snapshot.pair.pool_locator is None:
            payload["pair"].pop("pool_locator", None)
        rows.append(
            {
                "id": snapshot.id if snapshot.provider == IDENTITY.provider else uuid4(),
                "schema_version": snapshot.schema_version,
                "provider": snapshot.provider,
                "chain": snapshot.chain,
                "network": snapshot.network,
                "asset_id": snapshot.asset_id,
                "pair_id": snapshot.pair.pair_id,
                "correlation_id": snapshot.correlation_id,
                "observed_at": snapshot.observed_at,
                "recorded_at": snapshot.observed_at,
                "freshness_at": snapshot.freshness_at,
                "available": snapshot.available,
                "is_fixture": snapshot.is_fixture,
                "payload": payload,
            }
        )
    async with sessions.begin() as session:
        await session.execute(insert(MarketObservationRow), rows)


def other(at, **kwargs):
    """The same pool, as another provider observed it."""
    return foreign(held(at, **kwargs), provider="another-provider")


def exit_service(sessions, at):
    clock = FixedClock(at)
    markets = MarketReader(sessions, clock=clock)
    return markets, build_exit_service(
        sessions,
        at,
        feed=markets,
        exit_read=AtlasExitRead(builder=builder_for(at), clock=clock),
    )


def sweeper(sessions, at):
    markets, exits = exit_service(sessions, at)
    return AutoExitService(
        sessions=sessions,
        exits=exits,
        markets=markets,
        policy=POLICY,
        limits=RiskLimits(),
        clock=FixedClock(at),
    )


async def exit_count(sessions):
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(TradeCaseExitRow))


async def open_position(sessions):
    async with sessions() as session:
        return await session.scalar(select(PositionRow).where(PositionRow.quantity > 0))


# ------------------------------------------------------------- the reader


async def test_the_own_fresh_reading_is_selected_over_a_newer_foreign_one(risk_db, now):
    _, sessions = risk_db
    at = now + LATER
    own = held(at - timedelta(seconds=20), price="1.00")
    await record(sessions, own, other(at, price="0.10"))
    reader = MarketReader(sessions, clock=FixedClock(at))

    found = await reader.latest_in(MarketScope.of(IDENTITY))

    assert found is not None and found.id == own.id
    assert found.provider == IDENTITY.provider
    # The unscoped read is unchanged for every other consumer: newest of any source.
    newest = await reader.latest(IDENTITY.pair_id)
    assert newest is not None and newest.provider == "another-provider"


async def test_no_own_reading_is_none_never_the_foreign_one(risk_db, now):
    _, sessions = risk_db
    at = now + LATER
    await record(sessions, other(at, price="0.10"))
    reader = MarketReader(sessions, clock=FixedClock(at))

    assert await reader.latest_in(MarketScope.of(IDENTITY)) is None


async def test_a_stale_own_reading_is_not_refreshed_by_a_foreign_one(risk_db, now):
    _, sessions = risk_db
    at = now + LATER
    await record(sessions, held(at - timedelta(minutes=5), price="1.00"), other(at, price="0.10"))
    reader = MarketReader(sessions, clock=FixedClock(at))

    assert await reader.latest_in(MarketScope.of(IDENTITY)) is None


@pytest.mark.parametrize(
    "difference",
    [
        {"provider": "another-provider"},
        {"network": "testnet"},
        {"chain": "bsc"},
        {"base_asset_id": "robinhood:mainnet:0x" + "c7" * 20},
        {"quote_asset_id": "robinhood:mainnet:0x" + "77" * 20},
        {"venue": "another-venue"},
        {"is_fixture": True},
    ],
    ids=["provider", "network", "chain", "base-asset", "quote-asset", "venue", "fixture"],
)
def test_the_identity_rule_separates_every_coordinate(difference):
    assert describes_market(IDENTITY, IDENTITY)
    assert not describes_market(IDENTITY.model_copy(update=difference), IDENTITY)
    assert not describes_market(IDENTITY, IDENTITY.model_copy(update=difference))


@pytest.mark.parametrize(
    "difference",
    [{"provider": "another-provider"}, {"network": "testnet"}, {"chain": "bsc"}],
    ids=["provider", "network", "chain"],
)
def test_the_scope_refuses_a_reading_of_another_source(now, difference):
    snapshot = held(now)
    assert MarketScope.of(IDENTITY).matches(snapshot)
    assert not MarketScope.of(IDENTITY).matches(foreign(snapshot, **difference))


def test_a_pool_locator_completes_an_identity_that_had_none_and_nothing_else(now):
    snapshot = held(now)
    observed = snapshot.pair.market_identity
    assert observed.pool_locator is not None
    legacy = observed.model_copy(update={"pool_locator": None})
    # A case without a locator accepts a reading that adds one ...
    assert describes_market(observed, legacy)
    # ... never the reverse, and never a different locator.
    assert not describes_market(legacy, observed)
    moved = observed.model_copy(
        update={
            "pool_locator": observed.pool_locator.model_copy(update={"value": "0x" + "99" * 20})
        }
    )
    assert not describes_market(moved, observed)


def test_a_reading_of_the_same_pool_for_another_asset_is_not_the_held_market(now):
    """Identical pool id, a different base asset: separated by the asset itself."""
    snapshot = held(now)
    scope = MarketScope(
        pair_id=IDENTITY.pair_id,
        provider=IDENTITY.provider,
        chain=IDENTITY.chain,
        network=IDENTITY.network,
        base_asset_id="robinhood:mainnet:0x" + "c7" * 20,
    )
    assert not scope.matches(snapshot)
    assert MarketScope.of(IDENTITY).matches(snapshot)


# ---------------------------------------------------------- normal exits


async def test_a_normal_stop_executes_on_its_own_reading_beside_a_newer_foreign_one(
    risk_db, now, trace
):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + LATER
    await record(sessions, held(at - timedelta(seconds=20), price="0.90"), other(at, price="5.00"))

    result = await sweeper(sessions, at).sweep()

    assert result.triggers == {"STOP_LOSS": 1} and result.executed == 1, result
    async with sessions() as session:
        row = await session.scalar(select(TradeCaseExitRow))
    # Sold on the held market's own reading: its price, not the foreign 5.00.
    assert Decimal(row.basis["market_snapshot"]["price_usd"]) == Decimal("0.90")


async def test_without_an_own_reading_a_normal_exit_books_nothing(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + LATER
    await record(sessions, other(at, price="0.10", liquidity="1"))

    result = await sweeper(sessions, at).sweep()

    assert result.executed == 0, result
    assert await exit_count(sessions) == 0


async def test_a_normal_exit_on_a_stale_own_reading_books_nothing(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + LATER
    await record(sessions, held(at - timedelta(minutes=5), price="0.10"), other(at, price="0.10"))

    result = await sweeper(sessions, at).sweep()

    assert result.executed == 0, result
    assert await exit_count(sessions) == 0


async def test_the_sale_itself_uses_the_own_reading(risk_db, now, trace):
    _, sessions = risk_db
    _, _, position = await entered(sessions, now, trace)
    at = now + LATER
    await record(sessions, held(at - timedelta(seconds=20), price="1.10"), other(at, price="9.00"))
    _, exits = exit_service(sessions, at)

    sale = await exits.execute_position_exit(position.id, request_key="direct")

    assert sale.kind == "paper_exit_recorded", getattr(sale, "reason", None)
    async with sessions() as session:
        row = await session.scalar(select(TradeCaseExitRow))
    assert Decimal(row.basis["market_snapshot"]["price_usd"]) == Decimal("1.10")


async def test_a_blocked_exit_books_exactly_once_when_its_own_reading_arrives(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + LATER
    await record(sessions, other(at, price="0.10"))
    assert (await sweeper(sessions, at).sweep()).executed == 0

    later = at + timedelta(minutes=1)
    await record(sessions, held(later, price="0.90"))
    first = await sweeper(sessions, later).sweep()
    again = await sweeper(sessions, later + timedelta(minutes=1)).sweep()

    assert first.executed == 1
    assert again.executed == 0
    assert await exit_count(sessions) == 1


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required")
async def test_racing_sweeps_beside_a_foreign_source_book_once(risk_db, now, trace):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + LATER
    await record(sessions, held(at - timedelta(seconds=20), price="0.90"), other(at, price="5.00"))

    await asyncio.gather(*(sweeper(sessions, at).sweep() for _ in range(3)))

    # One sale in the ledger. (The normal sweep counts a replay of its own key
    # as executed; the database holds exactly one exit for the cycle.)
    assert await exit_count(sessions) == 1


# ------------------------------------------------------------ early exits


async def _early(sessions, now):
    from tests.early.test_exit import early_entry

    await early_entry(sessions, now)


def _early_sweeper(sessions, at):
    from tests.early.test_exit import sweeper as early_sweeper

    return early_sweeper(sessions, at)


async def test_an_early_stop_executes_on_its_own_reading_beside_a_newer_foreign_one(risk_db, now):
    _, sessions = risk_db
    await _early(sessions, now)
    at = now + LATER
    await record(sessions, held(at - timedelta(seconds=20), price="0.50"), other(at, price="5.00"))

    result = await _early_sweeper(sessions, at).sweep()

    assert result.triggers == {"STOP_LOSS": 1} and result.executed == 1, result


async def test_early_liquidity_is_only_the_held_market_s_own(risk_db, now):
    _, sessions = risk_db
    await _early(sessions, now)
    at = now + LATER
    # Own reading healthy, a newer foreign one thin: no invalidation.
    await record(
        sessions,
        held(at - timedelta(seconds=20), price="1.30", liquidity="750000"),
        other(at, price="1.30", liquidity="5000"),
    )
    assert (await _early_sweeper(sessions, at).sweep()).executed == 0

    # Own reading thin: invalidation, on the held market's own evidence.
    later = at + timedelta(minutes=1)
    await record(sessions, held(later, price="1.30", liquidity="9000"))
    result = await _early_sweeper(sessions, later).sweep()
    assert result.triggers == {"LIQUIDITY_INVALIDATION": 1} and result.executed == 1


async def test_a_seventy_two_hour_exit_runs_once_own_data_is_back(risk_db, now):
    _, sessions = risk_db
    await _early(sessions, now)
    at = now + timedelta(hours=72)
    await record(sessions, other(at, price="1.30"))

    blocked = await _early_sweeper(sessions, at).sweep()
    assert blocked.triggers == {"TIME_EXIT": 1} and blocked.executed == 0

    later = at + timedelta(minutes=1)
    await record(sessions, held(later, price="1.30"))
    done = await _early_sweeper(sessions, later).sweep()
    assert done.executed == 1
    assert await exit_count(sessions) == 1


def test_scopes_are_built_from_what_the_position_recorded():
    from src.core.models import Position

    position = Position(
        created_at=datetime(2026, 9, 9, tzinfo=UTC),
        updated_at=datetime(2026, 9, 9, tzinfo=UTC),
        source="LEDGER",
        correlation_id=uuid4(),
        asset_id=IDENTITY.base_asset_id,
        market_pair_id=IDENTITY.pair_id,
        market_chain=IDENTITY.chain,
        market_network=IDENTITY.network,
        market_provider=IDENTITY.provider,
        quantity=Decimal(1),
        cost_basis_usd=Decimal(1),
        realized_pnl_usd=Decimal(0),
    )
    scope = MarketScope.held(position)
    assert scope == MarketScope(
        pair_id=IDENTITY.pair_id,
        provider=IDENTITY.provider,
        chain=IDENTITY.chain,
        network=IDENTITY.network,
        base_asset_id=IDENTITY.base_asset_id,
    )
    assert MarketScope.held(position.model_copy(update={"market_pair_id": None})) is None


async def test_the_recorder_path_and_the_scoped_reader_agree(risk_db, now):
    _, sessions = risk_db
    at = now + LATER
    own = held(at, price="1.00")
    await MarketRecorder(sessions, clock=FixedClock(at)).record(own)
    reader = MarketReader(sessions, clock=FixedClock(at))
    found = await reader.latest_in(MarketScope.of(IDENTITY))
    assert found is not None and found.id == own.id
    assert (await open_position(sessions)) is None


# ------------------------------- same provider and pool, another full market

OTHER_BASE = "robinhood:mainnet:0x" + "c7" * 20
OTHER_QUOTE = "robinhood:mainnet:0x" + "d8" * 20


def _deep(value, old, new):
    if isinstance(value, dict):
        return {key: _deep(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_deep(item, old, new) for item in value]
    return new if value == old else value


def _row(snapshot, payload=None, **columns):
    payload = payload if payload is not None else snapshot.model_dump(mode="json")
    if snapshot.pair.pool_locator is None:
        payload["pair"].pop("pool_locator", None)
    row = {
        "id": uuid4(),
        "schema_version": snapshot.schema_version,
        "provider": snapshot.provider,
        "chain": snapshot.chain,
        "network": snapshot.network,
        "asset_id": snapshot.asset_id,
        "pair_id": snapshot.pair.pair_id,
        "correlation_id": snapshot.correlation_id,
        "observed_at": snapshot.observed_at,
        "recorded_at": snapshot.observed_at,
        "freshness_at": snapshot.freshness_at,
        "available": snapshot.available,
        "is_fixture": snapshot.is_fixture,
        "payload": payload,
    }
    row.update(columns)
    return row


async def _insert(sessions, *rows):
    async with sessions.begin() as session:
        await session.execute(insert(MarketObservationRow), list(rows))


def _another_market(snapshot, coordinate):
    """The same provider's newer reading of the same pool id, for another market."""
    payload = snapshot.model_dump(mode="json")
    if coordinate == "chain":
        return _row(snapshot, payload, chain="bsc")
    if coordinate == "network":
        return _row(snapshot, payload, network="testnet")
    if coordinate == "base":
        payload = _deep(payload, IDENTITY.base_asset_id, OTHER_BASE)
        MarketSnapshot.model_validate(payload)
        return _row(snapshot, payload, asset_id=OTHER_BASE)
    if coordinate == "quote":
        payload = _deep(payload, IDENTITY.quote_asset_id, OTHER_QUOTE)
        MarketSnapshot.model_validate(payload)
        return _row(snapshot, payload)
    if coordinate == "venue":
        payload = _deep(payload, IDENTITY.venue, "another-venue")
        MarketSnapshot.model_validate(payload)
        return _row(snapshot, payload)
    if coordinate == "locator":
        # A different locator under one pool id is not a valid snapshot (the
        # locator is bound to the pool). The valid boundary is a reading
        # without a locator, which a located market must not take as its own.
        payload["pair"].pop("pool_locator")
        payload["schema_version"] = 1
        payload.pop("quote_price", None)
        MarketSnapshot.model_validate(payload)
        return _row(snapshot, payload, schema_version=1)
    raise AssertionError(coordinate)


@pytest.mark.parametrize("coordinate", ["chain", "network", "base", "quote", "venue", "locator"])
async def test_a_newer_reading_of_another_market_never_hides_the_held_one(risk_db, now, coordinate):
    _, sessions = risk_db
    at = now + LATER
    own = held(at - timedelta(seconds=20), price="1.00")
    newer = held(at, price="9.00", label=f"newer-{coordinate}")
    await _insert(sessions, _row(own), _another_market(newer, coordinate))
    identity = own.pair.market_identity
    reader = MarketReader(sessions, clock=FixedClock(at))

    found = await reader.latest_in(MarketScope.of(identity))

    assert found is not None, coordinate
    assert found.id == own.id and found.price.value_usd == Decimal("1.00")


async def test_an_unavailable_newest_own_reading_is_not_replaced_by_an_older_one(risk_db, now):
    _, sessions = risk_db
    at = now + LATER
    older = held(at - timedelta(seconds=20), price="1.00")
    newest = held(at, price="1.10", label="newest-unavailable")
    await _insert(sessions, _row(older), _row(newest, available=False))
    reader = MarketReader(sessions, clock=FixedClock(at))

    assert await reader.latest_in(MarketScope.of(older.pair.market_identity)) is None


async def test_a_stale_newest_own_reading_is_not_replaced_by_an_older_one(risk_db, now):
    _, sessions = risk_db
    at = now + LATER
    older = held(at - timedelta(minutes=2), price="1.00", age=timedelta(seconds=1))
    newest = held(at - timedelta(minutes=1), price="1.10", age=timedelta(seconds=1))
    await _insert(sessions, _row(older), _row(newest))
    reader = MarketReader(sessions, clock=FixedClock(at))

    assert await reader.latest_in(MarketScope.of(older.pair.market_identity)) is None


async def test_a_legacy_identity_without_locator_still_reads_a_reading_that_adds_one(risk_db, now):
    _, sessions = risk_db
    at = now + LATER
    own = held(at, price="1.00")
    await _insert(sessions, _row(own))
    legacy = own.pair.market_identity.model_copy(update={"pool_locator": None})
    reader = MarketReader(sessions, clock=FixedClock(at))

    found = await reader.latest_in(MarketScope.of(legacy))

    assert found is not None and found.id == own.id


@pytest.mark.parametrize("coordinate", ["base", "quote", "venue"])
async def test_a_normal_stop_runs_on_its_own_market_beside_a_newer_other_market(
    risk_db, now, trace, coordinate
):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + LATER
    own = held(at - timedelta(seconds=20), price="0.90")
    await _insert(sessions, _row(own), _another_market(held(at, price="5.00"), coordinate))

    result = await sweeper(sessions, at).sweep()

    assert result.triggers == {"STOP_LOSS": 1} and result.executed == 1, result


@pytest.mark.parametrize("coordinate", ["base", "quote", "venue"])
async def test_an_early_stop_runs_on_its_own_market_beside_a_newer_other_market(
    risk_db, now, coordinate
):
    _, sessions = risk_db
    await _early(sessions, now)
    at = now + LATER
    own = held(at - timedelta(seconds=20), price="0.50")
    await _insert(sessions, _row(own), _another_market(held(at, price="5.00"), coordinate))

    result = await _early_sweeper(sessions, at).sweep()

    assert result.triggers == {"STOP_LOSS": 1} and result.executed == 1, result


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required")
async def test_racing_sweeps_beside_another_market_of_the_same_provider_book_once(
    risk_db, now, trace
):
    _, sessions = risk_db
    await entered(sessions, now, trace)
    at = now + LATER
    own = held(at - timedelta(seconds=20), price="0.90")
    await _insert(sessions, _row(own), _another_market(held(at, price="5.00"), "quote"))

    await asyncio.gather(*(sweeper(sessions, at).sweep() for _ in range(3)))

    assert await exit_count(sessions) == 1


async def test_a_partial_scope_with_two_current_markets_is_ambiguous_not_answered(risk_db, now):
    """A holding that recorded no quote or venue: two current markets fit it."""
    _, sessions = risk_db
    at = now + LATER
    own = held(at - timedelta(seconds=20), price="1.00")
    partial = MarketScope(
        pair_id=IDENTITY.pair_id,
        provider=IDENTITY.provider,
        chain=IDENTITY.chain,
        network=IDENTITY.network,
        base_asset_id=IDENTITY.base_asset_id,
    )
    reader = MarketReader(sessions, clock=FixedClock(at))
    await _insert(sessions, _row(own))
    found = await reader.latest_in(partial)
    assert found is not None and found.id == own.id

    await _insert(sessions, _another_market(held(at, price="9.00"), "quote"))

    assert await reader.latest_in(partial) is None
    # The full identity is never ambiguous.
    full = await reader.latest_in(MarketScope.of(own.pair.market_identity))
    assert full is not None and full.id == own.id
