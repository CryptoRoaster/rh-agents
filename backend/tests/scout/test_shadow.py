"""JEV-0 shadow triage in the scout: one assessment per new watch, and no effect.

The shadow invariant is tested the only honest way: the same runs with the
fast provider switched off and on, compared field by field on everything a
decision could read.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.data.tables import (
    Base,
    DiscoveryWatchFastAssessmentRow,
    DiscoveryWatchRow,
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
from tests.riskrequest.conftest import seed_account
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

    def __init__(self, failure: ReasoningFailure | Exception | None = None) -> None:
        self.failure = failure
        self.requests: list[FastRequest] = []

    async def assess(self, request: FastRequest) -> FastResult:
        self.requests.append(request)
        if self.failure is not None:
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


async def fast_rows(sessions) -> list[DiscoveryWatchFastAssessmentRow]:
    async with sessions() as session:
        return list(
            (
                await session.scalars(
                    select(DiscoveryWatchFastAssessmentRow).order_by(
                        DiscoveryWatchFastAssessmentRow.reserved_at,
                        DiscoveryWatchFastAssessmentRow.id,
                    )
                )
            ).all()
        )


async def test_each_new_watch_gets_exactly_one_shadow_assessment(db):
    _, sessions = db
    fast = ScriptedFast()
    summary = await scout(
        sessions, T0, provider=MarketProvider(discovery=[young(0), young(1)]), fast=fast
    )

    rows = await fast_rows(sessions)
    assert summary.watches_created == 2
    assert (summary.shadow_assessments_started, summary.shadow_assessments_completed) == (2, 2)
    assert len(rows) == 2 and len(fast.requests) == 2
    for row in rows:
        assert row.status == "COMPLETED"
        assert (row.provider, row.model, row.model_version) == ("jev", "jev-1.13.0", "jev-1.13.0")
        assert row.question_version == QUESTION_VERSION
        assert row.input_schema_version == 1
        assert row.input_digest == input_digest(row.input_payload)
        assert set(row.answers) == set(QUESTIONS)
        assert row.failure_category is None and row.failure_reason_code is None
    # The questions are the versioned set, and none asks what to do with a market.
    assert set(fast.requests[0].questions) == set(QUESTIONS)
    for question in QUESTIONS.values():
        text = question.instructions.lower()
        assert not any(word in text for word in ("buy", "sell", "trade this", "invest"))


async def test_no_duplicate_next_run_and_no_backfill_of_older_watches(db):
    _, sessions = db
    fast = ScriptedFast()
    provider = MarketProvider(discovery=[young(0)], targeted=[young(0)])
    await scout(sessions, T0, provider=provider, fast=None)  # a watch from before JEV
    provider.discovery = [young(0), young(1)]
    await scout(sessions, T0 + HOUR, provider=provider, fast=fast)
    await scout(sessions, T0 + 2 * HOUR, provider=provider, fast=fast)

    rows = await fast_rows(sessions)
    # Only the watch opened while JEV was on, and only once.
    assert len(rows) == 1
    async with sessions() as session:
        opened_later = await session.scalar(
            select(DiscoveryWatchRow.id).where(DiscoveryWatchRow.pair_id.like("%c2c2%"))
        )
    assert rows[0].watch_id == opened_later
    assert len(fast.requests) == 1


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
    assert first.shadow_assessments_started == 2
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

    (first, second) = await fast_rows(sessions)
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


def test_the_input_is_deterministic_and_uses_no_names():
    from datetime import UTC, datetime
    from uuid import uuid4

    from src.markets.models import MarketSnapshot
    from src.scout.models import DiscoveryWatch
    from tests.riskdata.conftest import recorded_snapshot

    now = datetime(2026, 9, 28, 6, tzinfo=UTC)
    snapshot: MarketSnapshot = recorded_snapshot(
        now,
        age=timedelta(minutes=2),
        pair_id="robinhood:mainnet:contract_address:0x" + "0e" * 20,
        base_asset_id="robinhood:mainnet:0x" + "9e" * 20,
        label="SUPERMOON",
    )
    watch = DiscoveryWatch.model_construct(
        id=uuid4(), chain="robinhood", first_seen_at=now - timedelta(minutes=5)
    )
    one = build_input(watch, snapshot, None, now)
    two = build_input(watch, snapshot, None, now)
    assert one == two and input_digest(one) == input_digest(two)
    shown = repr(one)
    # No symbol, name or address is shown to the fast model.
    assert "SUPERMOON" not in shown and "0x" not in shown
    assert one["prior_observation"] == {"available": False}
    assert one["watch_age_minutes"] == 5


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
    return {"watches": [tuple(row) for row in watches], "counts": counts}


async def test_jev_on_or_off_changes_no_decision_input():
    """Same runs, fast provider off and on: watches, ORBIT and trading state identical."""
    results = []
    for fast in (None, ScriptedFast()):
        engine, sessions = await fresh_db()
        try:
            orbit = EchoOrbit()
            provider = MarketProvider(
                discovery=[young(index) for index in range(6)],
                targeted=[young(index) for index in range(6)],
            )
            limited = scout_settings(
                early_scout_max_new_watches_per_run=4, early_scout_max_orbit_reviews_per_run=2
            )
            for step in range(3):
                await scout(
                    sessions,
                    T0 + step * HOUR,
                    provider=provider,
                    orbit=orbit,
                    fast=fast,
                    settings=limited,
                )
            results.append(
                {
                    "state": await decision_state(sessions),
                    "orbit": [call.data["market_observation"]["pair_id"] for call in orbit.calls],
                }
            )
        finally:
            await engine.dispose()
    off, on = results
    assert on == off
    assert all(value == 0 for value in on["state"]["counts"].values())
