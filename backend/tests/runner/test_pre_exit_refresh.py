"""The pre-exit refresh in a run: the production stage, before every sale.

The run is the production `BoundedPaperRun` over the production stack, with the
production `PreRiskMarketRefresh` as each sweep's refresh port and scripted
provider bytes underneath. Only the chain read is the fixture one a test can
run. What is proven: each triggered sale observes the held market again — by
its stored locator, through the identity checks — after the chain read and
before the sale; a failed or foreign answer sells nothing; budgets stay
bounded; and the exit job still buys nothing.
"""

from dataclasses import replace
from datetime import timedelta

import httpx
from sqlalchemy import func, select

from src.core.clock import FixedClock
from src.core.models import RiskLimits
from src.data.tables import ExecutionRow, TradeCaseExitRow, TradeCaseRow
from src.markets.reader import MarketReader
from src.orchestration.exitpolicy.early import EarlyExitService
from src.orchestration.exitpolicy.policy import PaperExitPolicy
from src.orchestration.exitpolicy.service import AutoExitService
from src.orchestration.paperexit.exitread import AtlasExitRead
from src.runner.composition import RunnerPorts
from src.runner.models import RunMode
from src.runner.service import BoundedPaperRun
from tests.atlas.conftest import builder_for, holder_source_result
from tests.paperexit.conftest import build_exit_service
from tests.runner.conftest import stack_for
from tests.runner.provider import MarketProvider
from tests.runner.test_exits_once import ZERO, exit_settings
from tests.runner.test_position_refresh import answer, portfolio

POLICY = PaperExitPolicy(stop_loss_bps=2000, take_profit_bps=5000, max_holding_seconds=6 * 3600)


class LaterAnswers(MarketProvider):
    """Answers the run-start acquisition as scripted; every later batch differently.

    `later` replaces what `pools/multi` returns after the first batch; `status`
    fails every later batch with that HTTP status instead.
    """

    def __init__(self, *, later=None, later_status=None, **kwargs):
        super().__init__(**kwargs)
        self.later = later
        self.later_status = later_status

    def handle(self, request: httpx.Request) -> httpx.Response:
        if "/pools/multi/" in request.url.path and self.multi_requests:
            if self.later_status is not None:
                self.requests.append(request)
                self.paths.append(request.url.path)
                return httpx.Response(self.later_status, text="{}")
            if self.later is not None:
                self.targeted = {item["attributes"]["address"]: item for item in self.later}
        return super().handle(request)


class HoldersOfAnyToken:
    """The fixture holder source, answering for whichever token is asked about."""

    def __init__(self, at):
        self.at = at

    async def holder_facts(self, chain, token_address):
        return holder_source_result(self.at, token_address=token_address)


def sale(sessions, at):
    clock = FixedClock(at)
    markets = MarketReader(sessions, clock=clock)
    builder = builder_for(at)
    builder = replace(builder, holders=HoldersOfAnyToken(at))
    return markets, build_exit_service(
        sessions,
        at,
        feed=markets,
        exit_read=AtlasExitRead(builder=builder, clock=clock),
        costs=ZERO,
    )


def refreshing_stack(sessions, at, provider, *, normal=False, **overrides):
    """The production stack; each sweep given the run's own pre-risk stage."""
    settings = exit_settings(**overrides)
    if normal:
        settings = settings.model_copy(
            update={
                "paper_auto_exit_enabled": True,
                "paper_exit_stop_loss_bps": POLICY.stop_loss_bps,
                "paper_exit_take_profit_bps": POLICY.take_profit_bps,
                "paper_exit_max_holding_minutes": POLICY.max_holding_seconds // 60,
            }
        )
    stack = stack_for(sessions, settings, at, ports=RunnerPorts(market_http=provider.transport()))
    assert stack.pre_risk is not None
    markets, exits = sale(sessions, at)
    early = EarlyExitService(
        sessions=sessions,
        exits=exits,
        markets=markets,
        limits=RiskLimits(),
        max_exits=settings.paper_exit_max_per_run,
        clock=FixedClock(at),
        refresh=stack.pre_risk,
    )
    auto = None
    if normal:
        auto = AutoExitService(
            sessions=sessions,
            exits=sale(sessions, at)[1],
            markets=markets,
            policy=POLICY,
            limits=RiskLimits(),
            max_exits=settings.paper_exit_max_per_run,
            clock=FixedClock(at),
            refresh=stack.pre_risk,
        )
    return replace(stack, early_exits=early, exits=auto)


async def count(sessions, table):
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(table))


async def exit_run(sessions, at, provider, **kwargs):
    return await BoundedPaperRun(
        refreshing_stack(sessions, at, provider, **kwargs), mode=RunMode.EXITS_ONLY
    ).execute()


async def test_five_triggered_early_exits_each_refresh_before_selling_and_buy_nothing(risk_db, now):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=5)
    cases_before = await count(sessions, TradeCaseRow)
    fills_before = await count(sessions, ExecutionRow)
    at = now + timedelta(minutes=10)
    provider = MarketProvider(targeted=[answer(item, price="0.40") for item in held])

    summary = await exit_run(sessions, at, provider)

    report = summary.early_exits
    assert (report.triggered, report.executed) == (5, 5), report
    # One run-start batch, then exactly one refresh batch per sale.
    assert report.refresh_attempts == 5 and report.refresh_failures == 0
    assert len(provider.multi_requests) == 1 + 5
    assert report.refresh_provider_requests == 5
    assert report.reevaluated_triggers == ("STOP_LOSS",)
    assert report.max_mark_age_at_final_seconds is not None
    assert report.max_mark_age_at_final_seconds <= RiskLimits().max_snapshot_age_seconds
    assert report.max_atlas_read_seconds is not None and report.max_refresh_seconds is not None
    # The exit job still buys nothing and opens nothing.
    assert provider.discovery_requests == []
    assert (summary.cases_opened, summary.risk_requests, summary.fills) == (0, 0, 0)
    assert await count(sessions, TradeCaseRow) == cases_before
    assert await count(sessions, ExecutionRow) == fills_before + 5
    assert await count(sessions, TradeCaseExitRow) == 5


async def test_normal_and_early_exits_in_one_run_are_each_refreshed(risk_db, now):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=1, normal=1)
    at = now + timedelta(minutes=10)
    provider = MarketProvider(targeted=[answer(item, price="0.40") for item in held])

    summary = await exit_run(sessions, at, provider, normal=True)

    assert (summary.exits.executed, summary.exits.refresh_attempts) == (1, 1), summary.exits
    assert (summary.early_exits.executed, summary.early_exits.refresh_attempts) == (1, 1)
    async with sessions() as session:
        triggers = sorted(
            (row.exit_policy_version, row.exit_trigger)
            for row in (await session.scalars(select(TradeCaseExitRow))).all()
        )
    assert triggers == [("EARLY_PAPER_EXIT_V1", "STOP_LOSS"), ("PAPER_EXIT_V1", "STOP_LOSS")]


async def test_a_refresh_answered_as_another_venue_sells_nothing(risk_db, now):
    _, sessions = risk_db
    (held,) = await portfolio(sessions, now, early=1)
    at = now + timedelta(minutes=10)
    provider = LaterAnswers(
        targeted=[answer(held, price="0.40")],
        later=[answer(held, price="0.40", venue="another-venue")],
    )

    summary = await exit_run(sessions, at, provider)

    report = summary.early_exits
    assert report.executed == 0 and report.refusals == ("PRE_EXIT_REFRESH_FAILED",), report
    assert report.refresh_failures == 1
    assert await count(sessions, TradeCaseExitRow) == 0


async def test_a_refresh_meeting_a_503_sells_nothing_and_the_next_run_does(risk_db, now):
    _, sessions = risk_db
    (held,) = await portfolio(sessions, now, early=1)
    at = now + timedelta(minutes=10)
    failing = LaterAnswers(targeted=[answer(held, price="0.40")], later_status=503)

    first = await exit_run(sessions, at, failing)

    assert first.early_exits.executed == 0
    assert first.early_exits.refusals == ("PRE_EXIT_REFRESH_FAILED",)
    # One refresh request for the sale, no retry beyond the transport's own.
    assert len(failing.multi_requests) >= 2
    assert await count(sessions, TradeCaseExitRow) == 0

    later = at + timedelta(minutes=1)
    healthy = MarketProvider(targeted=[answer(held, price="0.40")])
    second = await exit_run(sessions, later, healthy)

    assert second.early_exits.executed == 1, second.early_exits
    assert await count(sessions, TradeCaseExitRow) == 1


async def test_the_per_run_exit_budget_bounds_the_refreshes_too(risk_db, now):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=5)
    at = now + timedelta(minutes=10)
    provider = MarketProvider(targeted=[answer(item, price="0.40") for item in held])

    summary = await exit_run(sessions, at, provider, paper_exit_max_per_run=2)

    report = summary.early_exits
    assert (report.triggered, report.executed, report.refresh_attempts) == (5, 2, 2), report
    assert "EXIT_BUDGET_REACHED" in report.refusals
    assert len(provider.multi_requests) == 1 + 2
    assert await count(sessions, TradeCaseExitRow) == 2


async def test_the_refresh_figures_are_in_the_printed_summary(risk_db, now):
    _, sessions = risk_db
    held = await portfolio(sessions, now, early=1)
    at = now + timedelta(minutes=10)
    provider = MarketProvider(targeted=[answer(item, price="0.40") for item in held])

    summary = await exit_run(sessions, at, provider)

    printed = summary.model_dump(mode="json")["early_exits"]
    for key in (
        "refresh_attempts",
        "refresh_failures",
        "refresh_provider_requests",
        "triggers_cleared",
        "triggers_changed",
        "reevaluated_triggers",
        "max_refresh_seconds",
        "max_atlas_read_seconds",
        "max_mark_age_at_trigger_seconds",
        "max_mark_age_at_final_seconds",
    ):
        assert key in printed
    # Counts, codes and seconds: no URL.
    assert "://" not in summary.model_dump_json()
