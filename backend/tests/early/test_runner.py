"""The early stage of a bounded PAPER run: composed only under its own consent.

No provider is called: the refresh reports its provider as not configured, the
intake reads recorded observations, and the case it opens is a TradeCase of the
early workflow carrying the strategy's id — never one the scout opened.
"""

from datetime import timedelta
from types import SimpleNamespace

from src.orchestration.strategy.early import EARLY_WORKFLOW_VERSION, PRE_VECTOR_EARLY_ENTRY_V1
from src.runner.models import EarlyEntryReport
from src.runner.service import BoundedPaperRun
from tests.early.test_candidates import watching
from tests.runner.conftest import cases_in, run, runner_settings, stack_for
from tests.scout.test_promotion import NOW, promotable


def early_settings(**overrides):
    values = {
        "early_scout_enabled": True,
        "atlas_funding_graph_enabled": True,
        "pre_vector_early_entry_enabled": True,
        "early_paper_exit_enabled": True,
        "paper_runner_max_candidates": 2,
        "paper_runner_max_new_cases": 2,
        "paper_runner_max_cases": 2,
    }
    return runner_settings(**{**values, **overrides})


async def test_the_flag_is_off_by_default_and_composes_nothing(risk_db):
    _, sessions = risk_db
    stack = stack_for(sessions, runner_settings(early_scout_enabled=True), NOW)
    assert stack.early_intake is None
    assert stack.early_promotion is None


async def test_the_early_stage_opens_an_early_case_from_a_young_watching_watch(risk_db):
    _, sessions = risk_db
    watch = await watching(sessions, 0)

    summary = await run(sessions, early_settings(), NOW)

    assert summary.early is not None
    assert summary.early.cases_opened == 1
    assert summary.early.refresh_stop == "PROMOTION_REFRESH_PROVIDER_NOT_CONFIGURED"
    [case] = await cases_in(sessions)
    assert case.market_key == watch.pair_id
    assert case.strategy_policy_id == PRE_VECTOR_EARLY_ENTRY_V1
    assert case.workflow_version == EARLY_WORKFLOW_VERSION


async def test_a_promotable_watch_still_opens_a_normal_case(risk_db):
    _, sessions = risk_db
    watch = await promotable(sessions, 0)

    summary = await run(sessions, early_settings(), NOW)

    assert summary.cases_opened == 1
    assert summary.early.cases_opened == 0
    [case] = await cases_in(sessions)
    assert case.market_key == watch.pair_id
    assert case.strategy_policy_id is None
    assert case.workflow_version == "trade-case-v2"


async def test_one_early_case_per_run(risk_db):
    _, sessions = risk_db
    await watching(sessions, 0, first_seen=NOW - timedelta(minutes=20))
    await watching(sessions, 1, first_seen=NOW - timedelta(minutes=10))

    summary = await run(sessions, early_settings(), NOW)

    assert summary.early.cases_opened == 1
    assert len(await cases_in(sessions)) == 1


def test_one_early_fill_per_run_is_checked_before_anything_is_asked():
    early = SimpleNamespace(strategy_policy_id=PRE_VECTOR_EARLY_ENTRY_V1)
    normal = SimpleNamespace(strategy_policy_id=None)
    fresh = SimpleNamespace(early=EarlyEntryReport())
    spent = SimpleNamespace(early=EarlyEntryReport(fills=1))
    limited = BoundedPaperRun._early_entry_limited
    assert limited(None, early, fresh) is False  # type: ignore[arg-type]
    assert limited(None, early, spent) is True  # type: ignore[arg-type]
    assert limited(None, normal, spent) is False  # type: ignore[arg-type]
    assert limited(None, early, SimpleNamespace(early=None)) is False  # type: ignore[arg-type]


def test_early_entries_require_the_early_exit_contract():
    import pytest

    with pytest.raises(ValueError, match="EARLY_PAPER_EXIT_ENABLED"):
        early_settings(early_paper_exit_enabled=False)
    # The exit may run alone, so open early positions can be wound down.
    alone = runner_settings(early_paper_exit_enabled=True)
    assert alone.early_paper_exit_enabled and not alone.pre_vector_early_entry_enabled
    assert runner_settings().early_paper_exit_enabled is False
    with pytest.raises(ValueError, match="EARLY_PAPER_EXIT_ENABLED requires"):
        runner_settings(early_paper_exit_enabled=True, paper_runner_enabled=False)


async def test_the_early_exit_sweep_is_composed_only_under_its_flag(risk_db):
    _, sessions = risk_db
    assert stack_for(sessions, runner_settings(), NOW).early_exits is None
    stack = stack_for(sessions, runner_settings(early_paper_exit_enabled=True), NOW)
    assert stack.early_exits is not None
    assert stack.early_exits.policy.version == "EARLY_PAPER_EXIT_V1"


async def test_a_run_reports_the_early_exit_sweep(risk_db):
    _, sessions = risk_db
    off = await run(sessions, runner_settings(), NOW)
    assert off.early_exits is None
    on = await run(sessions, runner_settings(early_paper_exit_enabled=True), NOW)
    assert on.early_exits is not None
    assert (on.early_exits.evaluated, on.early_exits.executed) == (0, 0)
