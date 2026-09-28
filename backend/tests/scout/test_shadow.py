"""JEV-0 shadow triage at the discovery-candidate level, and its lack of any effect.

Every new valid stream a run discovers is assessed — the ones the watch limit
turns away included — and the assessment decides nothing. The invariant is
tested the only honest way: the same runs with the fast provider off, on and
failing, compared field by field on everything a decision could read.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.data.tables import (
    Base,
    DiscoveryFastAssessmentRow,
    DiscoveryStreamDeclineRow,
    DiscoveryWatchRow,
    MarketObservationRow,
    OrderRow,
    PositionRow,
    TradeCaseEvidenceRow,
    TradeCaseRiskRequestRow,
    TradeCaseRow,
    TradeRow,
)
from src.fast_reasoning.models import (
    ChoiceAnswer,
    FastRequest,
    FastResult,
    NoulAnswer,
    ScoreAnswer,
)
from src.reasoning.models import ReasoningErrorCategory, ReasoningFailure
from src.scout.shadow import QUESTION_VERSION, QUESTIONS, build_input, input_digest
from tests.atlas.conftest import QUOTE
from tests.riskrequest.conftest import seed_account
from tests.runner.provider import pool
from tests.scout.conftest import (
    HOUR,
    EchoOrbit,
    MarketProvider,
    scout,
    scout_settings,
    young,
)

T0 = datetime(2026, 9, 26, 6, tzinfo=UTC)


def answers_for(request: FastRequest) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, question in request.questions.items():
        if question.type == "noul":
            out[name] = NoulAnswer(noul=0.2)
        elif question.type == "choice":
            options = list(question.criteria)
            probabilities = {option: 0.0 for option in options}
            probabilities[options[0]] = 1.0
            out[name] = ChoiceAnswer(choice=options[0], probabilities=probabilities, confidence=1.0)
        else:
            levels = {str(index): text for index, text in enumerate(question.criteria)}
            probabilities = {key: 0.0 for key in levels}
            probabilities["1"] = 1.0
            out[name] = ScoreAnswer(
                score=1.0, legend=levels, probabilities=probabilities, confidence=1.0
            )
    return out


class ScriptedFast:
    """A fast provider that answers every question, or fails as told."""

    name = "jev"
    model = "jev-1.13.0"

    def __init__(
        self, failure: ReasoningFailure | Exception | None = None, *, fail_after: int = 0
    ) -> None:
        self.failure = failure
        self.fail_after = fail_after
        self.requests: list[FastRequest] = []

    async def assess(self, request: FastRequest) -> FastResult:
        self.requests.append(request)
        if self.failure is not None and len(self.requests) > self.fail_after:
            raise self.failure
        return FastResult(
            answers=answers_for(request),
            provider="jev",
            requested_model="jev-1.13.0",
            model_version="jev-1.13.0",
            input_tokens=300,
            output_tokens=20,
            latency_ms=40,
        )


def bsc_pool(index: int):
    return pool(
        "0x" + f"d{index}" * 20,
        base="0x" + f"e{index}" * 20,
        quote=QUOTE,
        price="0.001",
        liquidity="5000",
        volume="100",
        network="bsc",
    )


def both_chains(count: int = 10) -> MarketProvider:
    return MarketProvider(
        discovery_by_network={
            "robinhood": [young(index) for index in range(count)],
            "bsc": [bsc_pool(index) for index in range(count)],
        }
    )


BOTH = {"market_chains": "robinhood,bsc", "early_scout_max_new_watches_per_run": 10}


async def fast_rows(sessions) -> list[DiscoveryFastAssessmentRow]:
    async with sessions() as session:
        return list(
            (
                await session.scalars(
                    select(DiscoveryFastAssessmentRow).order_by(
                        DiscoveryFastAssessmentRow.reserved_at, DiscoveryFastAssessmentRow.id
                    )
                )
            ).all()
        )


async def count(sessions, table) -> int:
    async with sessions() as session:
        return int(await session.scalar(select(func.count()).select_from(table)) or 0)


async def test_every_new_candidate_is_assessed_not_only_the_watches(db):
    """20 discovered, 10 watch slots: 20 observed, 10 watches, 10 declined, 20 JEV."""
    _, sessions = db
    fast = ScriptedFast()
    summary = await scout(
        sessions, T0, provider=both_chains(), fast=fast, settings=scout_settings(**BOTH)
    )

    assert (summary.discovered, summary.valid_markets) == (20, 20)
    assert (summary.watches_created, summary.watches_declined) == (10, 10)
    assert summary.shadow_candidates == 20
    assert (summary.shadow_assessments_started, summary.shadow_assessments_completed) == (20, 20)
    assert await count(sessions, MarketObservationRow) == 20
    rows = await fast_rows(sessions)
    assert len(rows) == 20 and len(fast.requests) == 20
    # Both chains fully covered, and the watch slots split without starvation.
    assert sorted(row.chain for row in rows).count("bsc") == 10
    async with sessions() as session:
        watch_chains = (await session.scalars(select(DiscoveryWatchRow.chain))).all()
        declined = set((await session.scalars(select(DiscoveryStreamDeclineRow.pair_id))).all())
    assert (watch_chains.count("robinhood"), watch_chains.count("bsc")) == (5, 5)
    # The streams the limit turned away got their assessment all the same.
    assert len(declined) == 10
    assert declined <= {row.pair_id for row in rows}
    for row in rows:
        assert row.status == "COMPLETED"
        assert (row.provider, row.model, row.model_version) == ("jev", "jev-1.13.0", "jev-1.13.0")
        assert row.question_version == QUESTION_VERSION
        assert row.input_digest == input_digest(row.input_payload)
        assert set(row.answers) == set(QUESTIONS)


async def test_the_next_run_asks_no_one_twice_and_opens_no_declined_watch(db):
    _, sessions = db
    fast = ScriptedFast()
    settings = scout_settings(**BOTH)
    await scout(sessions, T0, provider=both_chains(), fast=fast, settings=settings)
    again = await scout(
        sessions, T0 + timedelta(minutes=15), provider=both_chains(), fast=fast, settings=settings
    )
    assert (again.watches_created, again.watches_declined, again.bootstrapped) == (0, 0, 0)
    assert again.shadow_candidates == 0
    assert again.shadow_assessments_started == 0
    assert len(fast.requests) == 20
    assert len(await fast_rows(sessions)) == 20
    assert await count(sessions, DiscoveryWatchRow) == 10


async def test_no_backfill_of_streams_known_before_jev(db):
    _, sessions = db
    fast = ScriptedFast()
    provider = MarketProvider(discovery=[young(0)], targeted=[young(0)])
    await scout(sessions, T0, provider=provider, fast=None)  # a watch from before JEV
    provider.discovery = [young(0), young(1)]
    await scout(sessions, T0 + HOUR, provider=provider, fast=fast)
    rows = await fast_rows(sessions)
    assert [row.pair_id for row in rows] == [young_pair(1)]


def young_pair(index: int) -> str:
    from tests.scout.conftest import POOLS, pair_id

    return pair_id(POOLS[index])


async def test_the_shadow_budget_is_bounded_per_run_and_per_day(db):
    _, sessions = db
    fast = ScriptedFast()
    tight = scout_settings(jev_max_assessments_per_run=2, jev_max_assessments_per_day=3)
    first = await scout(
        sessions,
        T0,
        provider=MarketProvider(discovery=[young(index) for index in range(4)]),
        fast=fast,
        settings=tight,
    )
    second = await scout(
        sessions,
        T0 + timedelta(minutes=15),
        provider=MarketProvider(discovery=[young(index) for index in range(4, 8)]),
        fast=fast,
        settings=tight,
    )
    assert (first.shadow_candidates, first.shadow_assessments_started) == (4, 2)
    assert (second.shadow_assessments_started, second.shadow_skipped_budget) == (1, 1)
    assert len(await fast_rows(sessions)) == 3


@pytest.mark.parametrize(
    ("failure", "category", "code"),
    [
        (
            ReasoningFailure(ReasoningErrorCategory.PROVIDER_TIMEOUT, "JEV_TIMEOUT"),
            "PROVIDER_TIMEOUT",
            "JEV_TIMEOUT",
        ),
        (
            ReasoningFailure(ReasoningErrorCategory.PROVIDER_RATE_LIMIT, "JEV_RATE_LIMITED"),
            "PROVIDER_RATE_LIMIT",
            "JEV_RATE_LIMITED",
        ),
        (
            ReasoningFailure(ReasoningErrorCategory.PROVIDER_UNAVAILABLE, "/secret/path token"),
            "PROVIDER_UNAVAILABLE",
            "UNCLASSIFIED",
        ),
        (RuntimeError("boom"), "PROVIDER_UNAVAILABLE", "FAST_PROVIDER_INTERNAL_ERROR"),
    ],
)
async def test_a_shadow_failure_is_recorded_and_blocks_nothing(db, failure, category, code):
    _, sessions = db
    orbit = EchoOrbit()
    fast = ScriptedFast(failure=failure)
    summary = await scout(
        sessions,
        T0,
        provider=MarketProvider(discovery=[young(0), young(1)]),
        orbit=orbit,
        fast=fast,
    )

    first, second = await fast_rows(sessions)
    assert first.status == second.status == "FAILED"
    assert (first.failure_category, first.failure_reason_code) == (category, code)
    assert first.answers is None
    # Discovery, the watches and ORBIT went on as without JEV; no retry, no Codex fallback.
    assert summary.errors == ()
    assert summary.watches_created == 2
    assert summary.orbit_reviews_completed == 2
    assert len(orbit.calls) == 2
    assert len(fast.requests) == 2
    assert summary.shadow_assessments_failed == 2
    assert summary.shadow_failure_codes == (code,)


def test_the_input_needs_no_watch_is_deterministic_and_uses_no_names():
    from src.markets.models import MarketSnapshot
    from tests.riskdata.conftest import recorded_snapshot

    now = datetime(2026, 9, 28, 6, tzinfo=UTC)
    snapshot: MarketSnapshot = recorded_snapshot(
        now,
        age=timedelta(minutes=2),
        pair_id="robinhood:mainnet:contract_address:0x" + "0e" * 20,
        base_asset_id="robinhood:mainnet:0x" + "9e" * 20,
        label="SUPERMOON",
    )
    one = build_input(snapshot, None, now - timedelta(minutes=5), now)
    two = build_input(snapshot, None, now - timedelta(minutes=5), now)
    assert one == two and input_digest(one) == input_digest(two)
    shown = repr(one)
    # No symbol, name or address is shown to the fast model; no watch state either.
    assert "SUPERMOON" not in shown and "0x" not in shown
    assert not any("watch" in key or "orbit" in key for key in one)
    assert one["prior_observation"] == {"available": False}
    assert one["stream_age_minutes"] == 5
    assert one["chain"] == "robinhood"


# ------------------------------------------------------------ the invariant


async def fresh_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    await seed_account(sessions)
    return engine, sessions


async def decision_state(sessions) -> dict[str, Any]:
    """Everything a decision could read, without identifiers that differ per database."""
    async with sessions() as session:
        watches = (
            await session.execute(
                select(
                    DiscoveryWatchRow.pair_id,
                    DiscoveryWatchRow.status,
                    DiscoveryWatchRow.next_orbit_review_at,
                    DiscoveryWatchRow.orbit_checkpoint_index,
                    DiscoveryWatchRow.next_history_review_at,
                    DiscoveryWatchRow.latest_vector_sufficiency,
                    DiscoveryWatchRow.reason_code,
                    DiscoveryWatchRow.last_promoted_trade_case_id,
                ).order_by(DiscoveryWatchRow.pair_id)
            )
        ).all()
        declined = sorted((await session.scalars(select(DiscoveryStreamDeclineRow.pair_id))).all())
        counts = {
            table.__tablename__: await session.scalar(select(func.count()).select_from(table))
            for table in (
                TradeCaseRow,
                TradeCaseRiskRequestRow,
                TradeRow,
                OrderRow,
                PositionRow,
                TradeCaseEvidenceRow,
            )
        }
    return {"watches": [tuple(row) for row in watches], "declined": declined, "counts": counts}


async def test_jev_off_on_or_failing_changes_no_decision_input():
    """Same runs with JEV off, on, and failing from the 11th call: identical decisions."""
    results = []
    variants = (
        None,
        ScriptedFast(),
        ScriptedFast(
            ReasoningFailure(ReasoningErrorCategory.PROVIDER_UNAVAILABLE, "JEV_OVERLOADED"),
            fail_after=10,
        ),
    )
    for fast in variants:
        engine, sessions = await fresh_db()
        try:
            orbit = EchoOrbit()
            settings = scout_settings(**BOTH, early_scout_max_orbit_reviews_per_run=2)
            for step in range(3):
                await scout(
                    sessions,
                    T0 + step * HOUR,
                    provider=both_chains(),
                    orbit=orbit,
                    fast=fast,
                    settings=settings,
                )
            results.append(
                {
                    "state": await decision_state(sessions),
                    "orbit": [call.data["market_observation"]["pair_id"] for call in orbit.calls],
                }
            )
            if fast is not None:
                rows = await fast_rows(sessions)
                assert len(rows) == 20
        finally:
            await engine.dispose()
    off, on, failing = results
    assert on == off
    assert failing == off
    assert all(value == 0 for value in on["state"]["counts"].values())
