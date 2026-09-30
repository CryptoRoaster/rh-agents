"""The pre-risk market refresh: exact-locator observations just before SENTINEL.

Two levels. The stage itself, built from the production transport, network
directory, adapter, normalization and recorder over scripted HTTP bytes — which
proves which markets it reads, how it binds them, what it refuses and what it
spends. And the run, composed by the production `build_stack`, with the risk
service wrapped only to observe *when* it is asked and what had been recorded by
then — which proves the order: market, risk; and market, risk, source refresh,
market, risk.

No real provider is called. A fixture proves what this system does with an
answer of that shape, never that the real provider gives one.
"""

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from src.core.clock import FixedClock
from src.core.models import RiskLimits
from src.data.repository import aware
from src.data.tables import TradeCaseRow
from src.markets.geckoterminal.adapter import GeckoTerminalAdapter
from src.markets.geckoterminal.networks import CHAINS, NetworkDirectory
from src.markets.geckoterminal.transport import GeckoTerminalTransport
from src.markets.models import Availability, MarketSnapshot
from src.markets.reader import MarketReader
from src.markets.recorder import MarketRecorder, record_pair_reporting
from src.orchestration.riskrequest.models import RiskRequestRefusal, RiskRequestRefused
from src.runner import pre_risk
from src.runner.models import AcquisitionOutcome
from src.runner.pre_risk import PreRiskLimits, PreRiskMarketRefresh, PreRiskReason
from src.runner.service import BoundedPaperRun, Deadline
from tests.atlas.conftest import QUOTE, TOKEN
from tests.runner.conftest import runner_settings, stack_for
from tests.runner.provider import POOL, MarketProvider, payment, pool, traded
from tests.runner.specialists import ScriptedSpecialists
from tests.runner.test_acquisition import (
    CHAIN,
    NETWORK,
    PAIR_ID,
    PAYMENT_PAIR_ID,
    acquiring_ports,
    acquiring_settings,
    full_acquiring_settings,
    hold,
    observations,
)
from tests.runner.test_end_to_end import SPOT, traded_case

OTHER_POOL = "0x" + "c4" * 20
OTHER_BASE = "0x" + "c3" * 20
OTHER_PAIR_ID = f"{CHAIN}:{NETWORK}:contract_address:{OTHER_POOL}"
STALE = timedelta(seconds=60)


def other(price: str = "2", **kwargs):
    return pool(OTHER_POOL, base=OTHER_BASE, quote=QUOTE, price=price, **kwargs)


async def seed(sessions, at, pools) -> None:
    """Record these pools at that instant, through the production adapter."""
    provider = MarketProvider(discovery=pools)
    settings = acquiring_settings()
    clock = FixedClock(at)
    transport = GeckoTerminalTransport(settings, transport=provider.transport(), clock=clock)
    try:
        adapter = GeckoTerminalAdapter(
            transport,
            NetworkDirectory(transport, settings),
            CHAINS[CHAIN],
            settings,
            clock=clock,
        )
        recorder = MarketRecorder(sessions, clock=clock)
        for pair in await adapter.discover():
            await record_pair_reporting(adapter, pair, recorder)
    finally:
        await transport.__aexit__(None, None, None)


def reader(sessions, now) -> MarketReader:
    return MarketReader(sessions, clock=FixedClock(now), max_age=timedelta(hours=1))


async def identity(sessions, now, pair_id=PAIR_ID):
    (found,) = await reader(sessions, now).identities([pair_id])
    return found


def stage(sessions, now, provider, *, requests=3, seconds=15, http=None, **overrides):
    settings = acquiring_settings(**overrides)
    return PreRiskMarketRefresh(
        settings,
        sessions,
        reader(sessions, now),
        RiskLimits(),
        PreRiskLimits(max_requests=requests, max_seconds=seconds),
        clock=FixedClock(now),
        http=http if http is not None else provider.transport(),
    )


def case_on(market):
    """Only the market is read off a case; the rest is the workflow's business."""
    return SimpleNamespace(market=market)


# ----------------------------------------------------------------- the market set


async def test_the_case_market_and_two_positions_are_read_once_each(risk_db, now, trace):
    """Case market plus two holdings elsewhere, and a third in the case's own market."""
    _, sessions = risk_db
    await seed(sessions, now - STALE, [traded(SPOT), payment(), other()])
    await hold(sessions, now, trace, pair_id=PAYMENT_PAIR_ID, asset_id=f"{CHAIN}:{NETWORK}:{QUOTE}")
    await hold(
        sessions, now, trace, pair_id=OTHER_PAIR_ID, asset_id=f"{CHAIN}:{NETWORK}:{OTHER_BASE}"
    )
    await hold(sessions, now, trace, pair_id=PAIR_ID, asset_id=f"{CHAIN}:{NETWORK}:{TOKEN}")
    provider = MarketProvider(targeted=[traded(SPOT), payment(), other()])

    reading = await stage(sessions, now, provider).refresh(
        case_on(await identity(sessions, now)), Deadline(60)
    )

    assert reading.ready, reading
    assert reading.markets == (PAIR_ID, PAYMENT_PAIR_ID, OTHER_PAIR_ID)
    assert (reading.attempted, reading.recorded, reading.refused) == (3, 3, 0)
    # One exact-locator batch for the chain, each pool in it exactly once.
    (asked,) = provider.multi_requests
    addresses = asked.rsplit("/", 1)[-1].split(",")
    assert sorted(addresses) == sorted([POOL, "0x" + "ff" * 20, OTHER_POOL])
    assert provider.discovery_requests == []
    assert reading.provider_requests == 2  # the network directory, then the batch


async def test_one_position_that_cannot_be_named_means_no_request(risk_db, now, trace):
    _, sessions = risk_db
    await seed(sessions, now - STALE, [traded(SPOT)])
    never = f"{CHAIN}:{NETWORK}:contract_address:" + "0x" + "99" * 20
    await hold(
        sessions, now, trace, pair_id=never, asset_id=f"{CHAIN}:{NETWORK}:" + "0x" + "77" * 20
    )
    provider = MarketProvider(targeted=[traded(SPOT)])

    reading = await stage(sessions, now, provider).refresh(
        case_on(await identity(sessions, now)), Deadline(60)
    )

    assert not reading.ready
    assert reading.reason == PreRiskReason.MARKET_IDENTITY_UNKNOWN.value
    # Nothing is asked when the set cannot be completed.
    assert provider.paths == []


async def test_one_position_the_provider_does_not_return_means_no_request(risk_db, now, trace):
    _, sessions = risk_db
    await seed(sessions, now - STALE, [traded(SPOT), other()])
    await hold(
        sessions, now, trace, pair_id=OTHER_PAIR_ID, asset_id=f"{CHAIN}:{NETWORK}:{OTHER_BASE}"
    )
    provider = MarketProvider(targeted=[traded(SPOT)])

    reading = await stage(sessions, now, provider).refresh(
        case_on(await identity(sessions, now)), Deadline(60)
    )

    assert not reading.ready
    assert reading.reason == PreRiskReason.MARKET_NOT_RETURNED.value
    assert (reading.recorded, reading.refused) == (1, 1)


# ---------------------------------------------------------------- exact identity


async def test_a_pool_answered_as_a_different_market_is_refused(risk_db, now, trace):
    """Same address, different base asset: never stored against the case's market."""
    _, sessions = risk_db
    await seed(sessions, now - STALE, [traded(SPOT)])
    impostor = pool(POOL, base=OTHER_BASE, quote=QUOTE, price=SPOT)
    provider = MarketProvider(targeted=[impostor])

    reading = await stage(sessions, now, provider).refresh(
        case_on(await identity(sessions, now)), Deadline(60)
    )

    assert not reading.ready
    assert reading.reason == PreRiskReason.MARKET_IDENTITY_MISMATCH.value
    assert len(await observations(sessions, PAIR_ID)) == 1


async def test_a_different_venue_is_refused(risk_db, now, trace):
    _, sessions = risk_db
    await seed(sessions, now - STALE, [traded(SPOT)])
    provider = MarketProvider(
        targeted=[pool(POOL, base=TOKEN, quote=QUOTE, price=SPOT, venue="pancakeswap")]
    )

    reading = await stage(sessions, now, provider).refresh(
        case_on(await identity(sessions, now)), Deadline(60)
    )

    assert not reading.ready
    assert reading.reason == PreRiskReason.MARKET_IDENTITY_MISMATCH.value


async def test_another_pool_is_never_substituted(risk_db, now, trace):
    """Asked for one pool, answered with another: not returned, not replaced."""
    _, sessions = risk_db
    await seed(sessions, now - STALE, [traded(SPOT)])
    provider = MarketProvider(targeted=[other()])
    provider.targeted = {POOL: other()}

    reading = await stage(sessions, now, provider).refresh(
        case_on(await identity(sessions, now)), Deadline(60)
    )

    assert not reading.ready
    assert reading.reason in (
        PreRiskReason.MARKET_NOT_RETURNED.value,
        PreRiskReason.PROVIDER_FAILED.value,
    )
    assert await observations(sessions, OTHER_PAIR_ID) == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"chain": "bsc"}, PreRiskReason.CHAIN_NOT_CONFIGURED),
        ({"provider": "coingecko"}, PreRiskReason.MARKET_IDENTITY_UNKNOWN),
        ({"pool_locator": None}, PreRiskReason.POOL_LOCATOR_UNKNOWN),
    ],
)
async def test_an_unaddressable_market_is_refused_without_asking(
    risk_db, now, trace, change, reason
):
    _, sessions = risk_db
    await seed(sessions, now - STALE, [traded(SPOT)])
    provider = MarketProvider(targeted=[traded(SPOT)])
    market = (await identity(sessions, now)).model_copy(update=change)

    reading = await stage(sessions, now, provider).refresh(case_on(market), Deadline(60))

    assert not reading.ready
    assert reading.reason == reason.value
    assert provider.paths == [], "no symbol, name or address search in its place"


# ------------------------------------------------------------ freshness, liquidity


async def test_a_fresh_reading_makes_the_market_fresh(risk_db, now, trace):
    _, sessions = risk_db
    await seed(sessions, now - STALE, [traded(SPOT)])
    provider = MarketProvider(targeted=[traded(SPOT * Decimal("1.1"))])

    reading = await stage(sessions, now, provider).refresh(
        case_on(await identity(sessions, now)), Deadline(60)
    )

    assert reading.ready, reading
    latest = await reader(sessions, now).latest(PAIR_ID)
    assert latest is not None and latest.freshness_at == now


async def test_a_provider_failure_never_falls_back_on_the_old_reading(risk_db, now, trace):
    """The stored reading is still inside SENTINEL's bound, and still not used."""
    _, sessions = risk_db
    await seed(sessions, now - timedelta(seconds=5), [traded(SPOT)])
    provider = MarketProvider(targeted=[traded(SPOT)], status=503)

    reading = await stage(sessions, now, provider, geckoterminal_retry_delay_seconds=0).refresh(
        case_on(await identity(sessions, now)), Deadline(60)
    )

    assert not reading.ready
    assert reading.reason in (
        PreRiskReason.PROVIDER_FAILED.value,
        PreRiskReason.REQUEST_BUDGET_REACHED.value,
    )


async def test_a_replayed_event_with_an_old_source_time_stays_stale(
    risk_db, now, trace, monkeypatch
):
    """A replay writes nothing, and what is stored is judged on its own instant."""
    _, sessions = risk_db
    await seed(sessions, now - STALE, [traded(SPOT)])

    async def replay(*_args, **_kwargs):
        return AcquisitionOutcome.UNCHANGED, "EVENT_ALREADY_RECORDED"

    monkeypatch.setattr(pre_risk, "record_observed", replay)
    provider = MarketProvider(targeted=[traded(SPOT)])

    reading = await stage(sessions, now, provider).refresh(
        case_on(await identity(sessions, now)), Deadline(60)
    )

    assert not reading.ready
    assert reading.unchanged == 1
    assert reading.reason == PreRiskReason.MARKET_STILL_STALE.value
    (row,) = await observations(sessions, PAIR_ID)
    assert aware(row.observed_at) == now - STALE, "nothing was re-dated"


async def test_an_unknown_recording_outcome_means_no_request(risk_db, now, trace, monkeypatch):
    _, sessions = risk_db
    await seed(sessions, now - STALE, [traded(SPOT)])

    async def unknown(*_args, **_kwargs):
        return AcquisitionOutcome.UNKNOWN, "RECORD_OUTCOME_UNKNOWN"

    monkeypatch.setattr(pre_risk, "record_observed", unknown)
    provider = MarketProvider(targeted=[traded(SPOT)])

    reading = await stage(sessions, now, provider).refresh(
        case_on(await identity(sessions, now)), Deadline(60)
    )

    assert not reading.ready
    assert reading.reason == PreRiskReason.RECORD_OUTCOME_UNKNOWN.value


async def test_unknown_liquidity_stays_unknown(risk_db, now, trace):
    _, sessions = risk_db
    await seed(sessions, now - STALE, [traded(SPOT)])
    provider = MarketProvider(
        targeted=[pool(POOL, base=TOKEN, quote=QUOTE, price=SPOT, liquidity=None)]  # type: ignore[arg-type]
    )

    reading = await stage(sessions, now, provider).refresh(
        case_on(await identity(sessions, now)), Deadline(60)
    )

    # Fresh, and exactly as unknown as the provider said: the request's own
    # readiness check is what refuses it, by its own name.
    assert reading.ready, reading
    newest = (await observations(sessions, PAIR_ID))[-1]
    stored = MarketSnapshot.model_validate(newest.payload)
    assert stored.observed_at == now
    assert stored.liquidity.status is Availability.UNKNOWN
    assert stored.liquidity.value_usd is None
    # And never replaced by the older, usable reading.
    assert await reader(sessions, now).latest(PAIR_ID) is None


# ------------------------------------------------------------------------- bounds


async def test_its_request_budget_is_its_own_and_is_enforced(risk_db, now, trace):
    """One request allowed: the directory spends it, the batch is refused."""
    _, sessions = risk_db
    await seed(sessions, now - STALE, [traded(SPOT)])
    provider = MarketProvider(targeted=[traded(SPOT)])

    reading = await stage(sessions, now, provider, requests=1).refresh(
        case_on(await identity(sessions, now)), Deadline(60)
    )

    assert not reading.ready
    assert reading.reason == PreRiskReason.REQUEST_BUDGET_REACHED.value
    assert provider.multi_requests == []


async def test_it_does_not_spend_the_acquisition_budget(risk_db, now, trace):
    """An exhausted-looking acquisition budget does not bind this stage, nor it that."""
    _, sessions = risk_db
    await seed(sessions, now - STALE, [traded(SPOT)])
    provider = MarketProvider(targeted=[traded(SPOT)])
    refresh = stage(
        sessions,
        now,
        provider,
        paper_runner_acquisition_max_provider_requests=1,
        paper_runner_acquisition_max_http_attempts=1,
    )

    reading = await refresh.refresh(case_on(await identity(sessions, now)), Deadline(60))

    assert reading.ready, reading
    assert reading.provider_requests == 2
    assert refresh._settings.geckoterminal_max_requests == 3
    assert refresh._settings.geckoterminal_total_timeout_seconds <= 15


async def test_an_expired_window_asks_nothing(risk_db, now, trace):
    _, sessions = risk_db
    await seed(sessions, now - STALE, [traded(SPOT)])
    provider = MarketProvider(targeted=[traded(SPOT)])

    reading = await stage(sessions, now, provider).refresh(
        case_on(await identity(sessions, now)), Deadline(0)
    )

    assert not reading.ready
    assert reading.reason == PreRiskReason.TIME_BUDGET_REACHED.value
    assert provider.paths == []


async def test_a_slow_provider_is_cut_off_by_its_own_time_bound(risk_db, now, trace):
    _, sessions = risk_db
    await seed(sessions, now - STALE, [traded(SPOT)])
    provider = MarketProvider(targeted=[traded(SPOT)])

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return provider.handle(request)

    reading = await stage(
        sessions, now, provider, seconds=1, http=httpx.MockTransport(slow)
    ).refresh(case_on(await identity(sessions, now)), Deadline(60))

    assert not reading.ready
    assert reading.reason == PreRiskReason.TIME_BUDGET_REACHED.value


def test_its_settings_are_bounded_and_separate():
    defaults = runner_settings()
    assert defaults.paper_runner_pre_risk_market_max_requests == 3
    assert defaults.paper_runner_pre_risk_market_max_seconds == 15
    with pytest.raises(ValueError, match="pre-risk"):
        acquiring_settings(
            paper_runner_max_seconds=10,
            paper_runner_acquisition_max_seconds=10,
            paper_runner_pre_risk_market_max_seconds=11,
        )
    # SENTINEL's own bound is not touched by any of this.
    assert RiskLimits().max_snapshot_age_seconds == 30


# ------------------------------------------------------------------ in the run


class WatchedRisk:
    """The production risk service, with a note of what was recorded when asked."""

    def __init__(self, inner, sessions, *, answer=None) -> None:
        self._inner = inner
        self._sessions = sessions
        self._answer = answer
        self.seen: list[int] = []

    async def request_risk_evaluation(self, trade_case_id: UUID, *, request_key: str):
        self.seen.append(len(await observations(self._sessions, PAIR_ID)))
        if self._answer is not None:
            return self._answer(trade_case_id)
        return await self._inner.request_risk_evaluation(trade_case_id, request_key=request_key)


class FailsAfterFirstBatch(MarketProvider):
    """Answers the run-start acquisition, then fails every later batch."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        if "/pools/multi/" in request.url.path and self.multi_requests:
            self.paths.append(request.url.path)
            return httpx.Response(503, text="{}")
        return super().handle(request)


async def watched_run(sessions, settings, at, ports, **wrap):
    stack = stack_for(sessions, settings, at, ports=ports)
    risk = WatchedRisk(stack.risk, sessions, **wrap)
    summary = await BoundedPaperRun(replace(stack, risk=risk)).execute()
    return summary, risk


async def opened(sessions, now, model):
    await BoundedPaperRun(
        stack_for(
            sessions,
            full_acquiring_settings(pulse_worker_enabled=False, anchor_worker_enabled=False),
            now,
            ports=acquiring_ports(now, model, MarketProvider(discovery=[traded(SPOT), payment()])),
        )
    ).execute()
    case = await traded_case(sessions)
    assert case is not None
    return case


async def test_the_market_is_observed_and_committed_before_risk_is_asked(risk_db, now, trace):
    _, sessions = risk_db
    model = ScriptedSpecialists()
    case = await opened(sessions, now, model)
    later = now + timedelta(seconds=20)
    moved = MarketProvider(targeted=[traded(SPOT * Decimal("1.20")), payment()])

    summary, risk = await watched_run(
        sessions,
        full_acquiring_settings(paper_runner_acquisition_max_discovery_requests=0),
        later,
        acquiring_ports(later, model, moved),
    )

    # Opening pass, run-start acquisition, then the pre-risk refresh: SENTINEL
    # was asked once, and by then the third observation was already durable.
    assert risk.seen == [3], summary
    progress = next(item for item in summary.cases if item.trade_case_id == case)
    (refreshed,) = progress.market_refreshes
    assert (refreshed.ready, refreshed.recorded, refreshed.markets) == (True, 1, (PAIR_ID,))
    assert progress.risk_outcome == "APPROVE", progress
    # The run-start acquisition is reported apart from it.
    assert summary.acquisition.recorded == 2


async def test_a_source_refresh_is_followed_by_another_market_refresh(risk_db, now, trace):
    """Market, risk, refusal, ATLAS refresh, market again, risk again."""
    from tests.refresh.conftest import ATLAS_SOURCE, RECHECK, ports_at, refreshes
    from tests.refresh.test_refresh import waited

    _, sessions = risk_db
    model = ScriptedSpecialists()
    opening = MarketProvider(discovery=[traded(SPOT), payment()])
    first = await BoundedPaperRun(
        stack_for(
            sessions,
            full_acquiring_settings(),
            now,
            ports=ports_at(now, model, market_http=opening.transport()),
        )
    ).execute()
    await waited(first, sessions)

    later = now + RECHECK
    moved = MarketProvider(targeted=[traded(SPOT * Decimal("1.20")), payment()])
    summary, risk = await watched_run(
        sessions,
        full_acquiring_settings(paper_runner_acquisition_max_discovery_requests=0),
        later,
        ports_at(later, model, market_http=moved.transport()),
    )

    assert risk.seen == [3, 4], summary
    case = await traded_case(sessions)
    progress = next(item for item in summary.cases if item.trade_case_id == case)
    assert refreshes(progress).get(ATLAS_SOURCE) == "ORDERED", progress
    assert [item.ready for item in progress.market_refreshes] == [True, True]
    assert progress.risk_outcome == "APPROVE", progress


async def test_no_source_refresh_means_no_second_market_refresh(risk_db, now, trace):
    _, sessions = risk_db
    model = ScriptedSpecialists()
    case = await opened(sessions, now, model)
    later = now + timedelta(seconds=20)
    moved = MarketProvider(targeted=[traded(SPOT * Decimal("1.20")), payment()])

    def refused(trade_case_id):
        # A refusal that names no source a new observation could fix.
        return RiskRequestRefused(
            reason=RiskRequestRefusal.PORTFOLIO_MARKS_UNAVAILABLE,
            trade_case_id=trade_case_id,
            trade_case_status="READY_FOR_RISK",
        )

    summary, risk = await watched_run(
        sessions,
        full_acquiring_settings(paper_runner_acquisition_max_discovery_requests=0),
        later,
        acquiring_ports(later, model, moved),
        answer=refused,
    )

    assert len(risk.seen) == 1
    progress = next(item for item in summary.cases if item.trade_case_id == case)
    assert len(progress.market_refreshes) == 1
    assert progress.risk_refusal == "PORTFOLIO_MARKS_UNAVAILABLE"
    assert progress.refreshes == ()


async def test_a_failed_refresh_sends_no_request_even_over_a_young_reading(risk_db, now, trace):
    """The opening observation is twenty seconds old and still not used."""
    _, sessions = risk_db
    model = ScriptedSpecialists()
    case = await opened(sessions, now, model)
    later = now + timedelta(seconds=20)
    down = FailsAfterFirstBatch(targeted=[traded(SPOT * Decimal("1.20")), payment()])

    summary, risk = await watched_run(
        sessions,
        full_acquiring_settings(
            paper_runner_acquisition_max_discovery_requests=0,
            geckoterminal_retry_delay_seconds=0,
        ),
        later,
        acquiring_ports(later, model, down),
    )

    assert risk.seen == [], summary
    assert summary.risk_requests == 0
    progress = next(item for item in summary.cases if item.trade_case_id == case)
    assert progress.pre_risk_refusal in (
        PreRiskReason.PROVIDER_FAILED.value,
        PreRiskReason.REQUEST_BUDGET_REACHED.value,
    ), progress
    assert progress.risk_outcome is None


async def test_a_risk_approved_replay_is_not_refreshed(risk_db, now, trace):
    """Approved in one pass, cut off before the fill, replayed in the next."""
    _, sessions = risk_db
    model = ScriptedSpecialists()
    case = await opened(sessions, now, model)
    later = now + timedelta(seconds=20)
    settings = full_acquiring_settings(paper_runner_acquisition_max_discovery_requests=0)

    stack = stack_for(
        sessions,
        settings,
        later,
        ports=acquiring_ports(
            later, model, MarketProvider(targeted=[traded(SPOT * Decimal("1.20")), payment()])
        ),
    )

    class Interrupted:
        async def execute_case_fill(self, *_args, **_kwargs):
            raise TimeoutError

    approved = await BoundedPaperRun(replace(stack, fills=Interrupted())).execute()
    first = next(item for item in approved.cases if item.trade_case_id == case)
    assert first.risk_outcome == "APPROVE" and first.outcome_unknown, first

    async with sessions() as session:
        row = await session.get(TradeCaseRow, case)
        assert row is not None and row.status == "RISK_APPROVED"

    again = later + timedelta(seconds=1)
    replay = MarketProvider(targeted=[traded(SPOT * Decimal("1.20")), payment()])
    summary, risk = await watched_run(
        sessions, settings, again, acquiring_ports(again, model, replay)
    )

    progress = next(item for item in summary.cases if item.trade_case_id == case)
    # The stored verdict was replayed under the same key and acted on.
    assert progress.risk_outcome == "APPROVE", progress
    assert progress.market_refreshes == ()
    assert len(risk.seen) == 1
    # Only the run-start acquisition asked anything.
    assert len(replay.multi_requests) == 1, replay.paths
