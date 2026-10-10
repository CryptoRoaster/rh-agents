"""The pre-exit refresh: every automatic trigger judged again on fresh evidence.

A sweep decides a trigger on the marks recorded when it started. The exit's own
on-chain read then takes its time — here, a read that moves the clock forward
the way a slow chain does. The held market is observed again after that read,
the policy evaluated again on the new reading, and the sale bound to exactly
that reading and checked against SENTINEL's bound at the execution boundary.

Everything below the sweep is real: the early entry, the recorder and reader,
ATLAS's deterministic half, `PaperExitService`, SENTINEL and the ledger. The
refresh port is a stand-in that records what a provider would have answered —
the production stage (`PreRiskMarketRefresh`) is exercised end to end in
`tests/runner/test_pre_exit_refresh.py`.
"""

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from src.core.models import RiskLimits, TradingMode
from src.data.tables import AccountRow
from src.markets.models import MarketSnapshot
from src.markets.reader import MarketReader
from src.markets.recorder import MarketRecorder
from src.orchestration.exitpolicy.early import EarlyExitService
from src.orchestration.paper import PaperTradingService
from src.orchestration.paperexit.exitread import AtlasExitRead
from src.orchestration.workflow.service import TradeCaseService
from tests.atlas.conftest import builder_for
from tests.early.test_exit import ZERO, early_entry, exit_rows, held_quantity, observe
from tests.paperexit.conftest import build_exit_service, money
from tests.paperexit.test_market_identity import foreign
from tests.paperexit.test_scoped_markets import held as held_reading
from tests.paperexit.test_scoped_markets import record
from tests.riskdata.conftest import IDENTITY, recorded_snapshot

SLOW = timedelta(seconds=40)
POSTGRES = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL row locks required"
)


@dataclass
class MovingClock:
    """Service time that a slow read moves forward, shared by every service."""

    instant: datetime

    def now(self) -> datetime:
        return self.instant

    def advance(self, delta: timedelta) -> None:
        self.instant = self.instant + delta


@dataclass
class SlowRead:
    """ATLAS's deterministic exit read, taking `takes` of service time.

    `measured="end"` observes the chain when the read finishes, as a collector
    whose last call is the holder read does; `"start"` observes it when the read
    began, so the measurement itself is as old as the read is slow. `during`
    runs while the read is in flight — another job recording the market.
    """

    clock: MovingClock
    takes: timedelta = SLOW
    measured: str = "end"
    during: object = None
    calls: int = 0

    async def read(self, trade_case_id, market, request_key):
        self.calls += 1
        started = self.clock.now()
        self.clock.advance(self.takes)
        if self.during is not None:
            await self.during()
        at = self.clock.now() if self.measured == "end" else started
        return await AtlasExitRead(builder=builder_for(at), clock=self.clock).read(
            trade_case_id, market, request_key
        )


@dataclass
class ScriptedRefresh:
    """What the provider answers each time the held market is observed again.

    Each step is a price (a fresh reading of the held market is recorded at the
    service instant), a dict with `price` and optional `liquidity`/`foreign`,
    or a failure code (`"!PROVIDER_FAILED"`): nothing recorded, not ready.
    """

    sessions: object
    clock: MovingClock
    steps: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    async def refresh(self, trade_case, deadline, *, networks=None):
        self.calls.append(trade_case.market.pair_id)
        step = self.steps.pop(0) if self.steps else None
        if isinstance(step, str) and step.startswith("!"):
            return SimpleNamespace(ready=False, reason=step[1:], provider_requests=1)
        if step is not None:
            spec = step if isinstance(step, dict) else {"price": step}
            at = self.clock.now()
            reading = held_reading(
                at,
                price=spec["price"],
                liquidity=spec.get("liquidity"),
                age=timedelta(seconds=1),
                label=f"refresh-{at.isoformat()}-{len(self.calls)}",
            )
            if spec.get("foreign"):
                await record(self.sessions, stranger(reading, spec["foreign"], at))
            else:
                await MarketRecorder(self.sessions, clock=self.clock).record(reading)
        return SimpleNamespace(ready=True, reason=None, provider_requests=1)


OTHER_ASSET = "robinhood:mainnet:0x" + "c7" * 20
OTHER_QUOTE = "0x" + "77" * 20


def stranger(reading, kind, at):
    """A fresh reading of the held pool that is not the held market, by one coordinate."""
    if kind in ("provider", "chain"):
        return foreign(reading, **{kind: "another-provider" if kind == "provider" else "bsc"})
    if kind in ("base-asset", "quote-asset"):
        extra = (
            {"base_asset_id": OTHER_ASSET}
            if kind == "base-asset"
            else {"quote_address": OTHER_QUOTE}
        )
        return recorded_snapshot(
            at,
            age=timedelta(seconds=1),
            metadata_age=timedelta(seconds=1),
            price=reading.price.value_usd,
            label=f"stranger-{kind}-{at.isoformat()}",
            **extra,
        )
    payload = reading.model_dump(mode="json")
    payload["pair"]["venue"] = "another-venue"
    if payload["pair"].get("pool_locator"):
        payload["pair"]["pool_locator"]["venue"] = "another-venue"
    return MarketSnapshot.model_validate(payload)


def services(sessions, clock, *, read=None, refresh=None, paper_clock=None, **overrides):
    markets = MarketReader(sessions, clock=clock)
    exits = build_exit_service(
        sessions,
        clock.now(),
        feed=overrides.pop("feed", markets),
        exit_read=read if read is not None else SlowRead(clock),
        costs=ZERO,
        clock=clock,
        cases=TradeCaseService(sessions, clock=clock),
        paper=PaperTradingService(
            sessions, RiskLimits(), TradingMode.PAPER, clock=paper_clock or clock
        ),
    )
    if refresh is not None:
        overrides["refresh"] = refresh
    return EarlyExitService(
        sessions=sessions,
        exits=exits,
        markets=markets,
        limits=RiskLimits(),
        clock=clock,
        **overrides,
    )


async def stop_at(sessions, now, price="0.50", **extra):
    """An early position, and a fresh reading at `price` ten minutes later."""
    await early_entry(sessions, now)
    at = now + timedelta(minutes=10)
    await observe(sessions, at, price=price, **extra)
    return MovingClock(at)


# ------------------------------------------------------------- the trigger again


async def test_a_stop_whose_price_recovered_during_the_chain_read_sells_nothing(risk_db, now):
    """RED on aabfee5: the stop fired at 0.50 and the sale went through at 1.30."""
    _, sessions = risk_db
    clock = await stop_at(sessions, now)

    async def recovered():
        # Another job records the held market while the chain read is running.
        await observe(sessions, clock.now(), price="1.30")

    read = SlowRead(clock, takes=timedelta(seconds=10), during=recovered)
    result = await services(sessions, clock, read=read).sweep()

    assert result.triggers == {"STOP_LOSS": 1} and result.executed == 0, result
    assert result.refusals == {"EXIT_TRIGGER_CLEARED": 1}
    assert result.triggers_cleared == 1
    assert await exit_rows(sessions) == []
    assert money(await held_quantity(sessions)) == money(8)


async def test_a_stop_still_breached_after_the_refresh_sells_exactly_once(risk_db, now):
    _, sessions = risk_db
    clock = await stop_at(sessions, now)
    refresh = ScriptedRefresh(sessions, clock, ["0.45"])
    sweep = services(sessions, clock, refresh=refresh)

    result = await sweep.sweep()

    assert (result.triggered, result.executed) == (1, 1), result
    assert refresh.calls == [IDENTITY.pair_id]
    [row] = await exit_rows(sessions)
    assert row.exit_trigger == "STOP_LOSS"
    assert money(await held_quantity(sessions)) == money(0)
    # Sold on the refreshed reading, not on the one the trigger first saw.
    assert row.exit_trigger_basis["inputs"]["mark_price_usd"] == "0.45"
    assert row.exit_trigger_basis["original"]["inputs"]["mark_price_usd"] == "0.50"
    pre_exit = row.basis["pre_exit"]
    assert (pre_exit["original_trigger"], pre_exit["reevaluated_trigger"]) == (
        "STOP_LOSS",
        "STOP_LOSS",
    )
    assert pre_exit["trigger_cleared"] is False
    assert pre_exit["refresh_attempts"] == 1
    assert pre_exit["mark_age_at_reevaluation_seconds"] == 1.0
    assert 0 <= pre_exit["mark_age_at_final_seconds"] <= RiskLimits().max_snapshot_age_seconds
    assert pre_exit["atlas_read_seconds"] >= 0
    # And the sweep reports it.
    assert (result.refresh_attempts, result.refresh_failures, result.triggers_changed) == (1, 0, 0)
    assert result.reevaluated == {"STOP_LOSS": 1}
    assert result.max_mark_age_at_trigger_seconds == 5.0
    assert result.max_mark_age_at_final_seconds == 1.0

    again = services(sessions, clock, refresh=ScriptedRefresh(sessions, clock, ["0.45"]))
    assert (await again.sweep()).executed == 0
    assert len(await exit_rows(sessions)) == 1


async def test_a_refresh_that_shows_the_recovery_sells_nothing(risk_db, now):
    _, sessions = risk_db
    clock = await stop_at(sessions, now)

    result = await services(
        sessions, clock, refresh=ScriptedRefresh(sessions, clock, ["1.30"])
    ).sweep()

    assert result.refusals == {"EXIT_TRIGGER_CLEARED": 1} and result.executed == 0
    assert result.triggers_cleared == 1 and result.refresh_attempts == 1
    assert await exit_rows(sessions) == []


async def test_a_slow_read_without_a_refresh_leaves_the_mark_stale_and_sells_nothing(risk_db, now):
    _, sessions = risk_db
    clock = await stop_at(sessions, now)

    result = await services(sessions, clock).sweep()

    # The trigger's mark is 45 s old after a 40 s read: unknown, not cleared.
    assert result.refusals == {"PRE_EXIT_REFRESH_FAILED": 1}, result
    assert await exit_rows(sessions) == []


async def test_a_slow_read_followed_by_the_refresh_sells_on_a_fresh_mark(risk_db, now):
    _, sessions = risk_db
    clock = await stop_at(sessions, now)

    result = await services(
        sessions, clock, refresh=ScriptedRefresh(sessions, clock, ["0.50"])
    ).sweep()

    assert result.executed == 1, result
    [row] = await exit_rows(sessions)
    assert row.basis["pre_exit"]["mark_age_at_final_seconds"] == 1.0


async def test_new_illiquidity_takes_the_policy_s_own_priority(risk_db, now):
    """RED on aabfee5: the sale was recorded as the trigger it no longer was."""
    _, sessions = risk_db
    await early_entry(sessions, now)
    at = now + timedelta(hours=72)
    await observe(sessions, at, price="1.30")
    clock = MovingClock(at)

    async def drained():
        await observe(sessions, clock.now(), price="1.30", liquidity="5000")

    read = SlowRead(clock, takes=timedelta(seconds=10), during=drained)
    result = await services(sessions, clock, read=read).sweep()

    assert result.triggers == {"TIME_EXIT": 1} and result.executed == 1, result
    [row] = await exit_rows(sessions)
    assert row.exit_trigger == "LIQUIDITY_INVALIDATION"
    assert row.exit_trigger_basis["verdict"]["conditions"] == [
        "LIQUIDITY_INVALIDATION",
        "TIME_EXIT",
    ]
    assert row.exit_trigger_basis["original"]["verdict"]["trigger"] == "TIME_EXIT"
    assert result.triggers_changed == 1
    assert result.reevaluated == {"LIQUIDITY_INVALIDATION": 1}


@pytest.mark.parametrize(
    ("refreshed", "sold"),
    [("1.45", True), ("1.60", False)],
    ids=["still-below-half-the-peak", "recovered-above-half-the-peak"],
)
async def test_a_trailing_stop_is_judged_again_against_the_historical_peak(
    risk_db, now, refreshed, sold
):
    _, sessions = risk_db
    await early_entry(sessions, now)
    await observe(sessions, now + timedelta(hours=1), price="3.00")
    at = now + timedelta(hours=6)
    await observe(sessions, at, price="1.50")
    clock = MovingClock(at)

    result = await services(
        sessions, clock, refresh=ScriptedRefresh(sessions, clock, [refreshed])
    ).sweep()

    assert result.triggers == {"TRAILING_STOP": 1}
    if sold:
        assert result.executed == 1, result
        [row] = await exit_rows(sessions)
        verdict = row.exit_trigger_basis["verdict"]
        # The peak an hour after entry, never the current price alone.
        assert Decimal(verdict["peak_price_usd"]) == Decimal("3.00")
        assert row.exit_trigger_basis["inputs"]["peak"]["observations"] >= 3
    else:
        assert result.refusals == {"EXIT_TRIGGER_CLEARED": 1}
        assert await exit_rows(sessions) == []


async def test_a_time_exit_is_sold_on_fresh_data(risk_db, now):
    _, sessions = risk_db
    await early_entry(sessions, now)
    at = now + timedelta(hours=72)
    await observe(sessions, at, price="1.30")
    clock = MovingClock(at)

    result = await services(
        sessions, clock, refresh=ScriptedRefresh(sessions, clock, ["1.31"])
    ).sweep()

    assert result.triggers == {"TIME_EXIT": 1} and result.executed == 1, result
    [row] = await exit_rows(sessions)
    assert row.exit_trigger == "TIME_EXIT"
    assert row.basis["pre_exit"]["mark_age_at_final_seconds"] == 1.0


# ------------------------------------------------------------- final freshness


async def test_a_mark_that_ages_past_the_bound_before_booking_books_nothing(risk_db, now):
    _, sessions = risk_db
    clock = await stop_at(sessions, now)
    # The execution boundary is read 31 s after everything else: the decision
    # was taken on a fresh mark, and by the fill it is not fresh any more.
    late = MovingClock(clock.now() + SLOW + timedelta(seconds=31))

    result = await services(
        sessions, clock, refresh=ScriptedRefresh(sessions, clock, ["0.45"]), paper_clock=late
    ).sweep()

    assert result.executed == 0
    assert result.refusals == {"EXECUTION_WINDOW_EXPIRED": 1}, result
    assert await exit_rows(sessions) == []
    assert money(await held_quantity(sessions)) == money(8)


async def test_a_chain_read_that_is_itself_too_old_asks_no_provider(risk_db, now):
    _, sessions = risk_db
    clock = await stop_at(sessions, now)
    refresh = ScriptedRefresh(sessions, clock, ["0.45"])

    result = await services(
        sessions, clock, read=SlowRead(clock, measured="start"), refresh=refresh
    ).sweep()

    assert refresh.calls == []
    assert result.refusals == {"SOURCE_OLDER_THAN_RISK_LIMIT": 1}, result
    assert await exit_rows(sessions) == []


async def test_a_newer_reading_between_the_judgement_and_the_lock_is_not_sold_on(risk_db, now):
    _, sessions = risk_db
    clock = await stop_at(sessions, now)
    markets = MarketReader(sessions, clock=clock)

    class ArrivesBeforeTheLock:
        """The exit's own reads see one more reading than the sweep judged."""

        written = False

        async def latest(self, identity, *, include_fixtures=False):
            return await markets.latest(identity, include_fixtures=include_fixtures)

        async def latest_in(self, scope, *, include_fixtures=False):
            if not self.written:
                self.written = True
                # Observed after the reading the sweep judged, still fresh.
                clock.advance(timedelta(seconds=10))
                await observe(sessions, clock.now(), price="0.44")
            return await markets.latest_in(scope, include_fixtures=include_fixtures)

    result = await services(
        sessions,
        clock,
        refresh=ScriptedRefresh(sessions, clock, ["0.45"]),
        feed=ArrivesBeforeTheLock(),
    ).sweep()

    assert result.refusals == {"EXIT_EVIDENCE_CHANGED": 1}, result
    assert await exit_rows(sessions) == []


# ---------------------------------------------------------------- identity


@pytest.mark.parametrize("difference", ["provider", "chain", "base-asset", "quote-asset", "venue"])
async def test_a_refresh_of_another_market_is_never_the_held_market_s_mark(
    risk_db, now, difference
):
    _, sessions = risk_db
    clock = await stop_at(sessions, now)
    refresh = ScriptedRefresh(sessions, clock, [{"price": "0.40", "foreign": difference}])

    result = await services(sessions, clock, refresh=refresh).sweep()

    # The foreign reading is fresh and breached; the held market's own is 45 s
    # old. Nothing is judged — and nothing sold — on somebody else's evidence.
    assert result.refusals == {"PRE_EXIT_REFRESH_FAILED": 1}, result
    assert await exit_rows(sessions) == []
    assert money(await held_quantity(sessions)) == money(8)


# ------------------------------------------------------------- failures, budgets


@pytest.mark.parametrize("reason", ["PROVIDER_FAILED", "TIME_BUDGET_REACHED"])
async def test_a_failed_refresh_sells_nothing_and_says_why(risk_db, now, reason):
    _, sessions = risk_db
    clock = await stop_at(sessions, now)
    refresh = ScriptedRefresh(sessions, clock, [f"!{reason}"])

    result = await services(sessions, clock, refresh=refresh).sweep()

    assert result.refusals == {"PRE_EXIT_REFRESH_FAILED": 1}, result
    assert (result.refresh_attempts, result.refresh_failures) == (1, 1)
    # One attempt, no retry inside it.
    assert len(refresh.calls) == 1
    assert await exit_rows(sessions) == []


async def test_a_sale_refused_by_a_provider_outage_is_retried_by_the_next_run(risk_db, now):
    _, sessions = risk_db
    clock = await stop_at(sessions, now)
    failed = await services(
        sessions, clock, refresh=ScriptedRefresh(sessions, clock, ["!PROVIDER_FAILED"])
    ).sweep()
    assert failed.executed == 0

    clock.advance(timedelta(minutes=1))
    await observe(sessions, clock.now(), price="0.45")
    retried = await services(
        sessions, clock, refresh=ScriptedRefresh(sessions, clock, ["0.45"])
    ).sweep()

    assert retried.executed == 1, retried
    assert len(await exit_rows(sessions)) == 1


async def test_a_stop_in_force_asks_no_provider(risk_db, now):
    _, sessions = risk_db
    clock = await stop_at(sessions, now)
    async with sessions.begin() as session:
        await session.execute(update(AccountRow).values(paused=True))
    refresh = ScriptedRefresh(sessions, clock, ["0.45"])

    result = await services(sessions, clock, refresh=refresh).sweep()

    assert refresh.calls == []
    assert result.refusals == {"SYSTEM_PAUSED": 1}, result
    assert await exit_rows(sessions) == []


@POSTGRES
async def test_racing_sweeps_with_refreshes_book_one_exit(risk_db, now):
    _, sessions = risk_db
    clock = await stop_at(sessions, now)

    # Three processes: each its own clock, each its own slow read and refresh.
    clocks = [MovingClock(clock.now()) for _ in range(3)]
    results = await asyncio.gather(
        *(
            services(sessions, own, refresh=ScriptedRefresh(sessions, own, ["0.45"])).sweep()
            for own in clocks
        )
    )

    assert sum(item.executed for item in results) <= 1
    assert len(await exit_rows(sessions)) == 1
    assert money(await held_quantity(sessions)) == money(0)
