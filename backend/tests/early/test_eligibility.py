"""Which young markets the EARLY producer admits, and why it refuses the rest.

Through the real workflow, the real EARLY context reader and handler, and
VECTOR's own `assess`. The only stand-ins are the recorded market read and the
history provider.
"""

from datetime import timedelta

import pytest

from src.core.clock import FixedClock
from src.orchestration.workflow.service import TradeCaseService
from tests.early.conftest import (
    early_setup,
    open_early_case,
    record_early_onchain,
    young_history,
)
from tests.riskdata.conftest import RecordedMarkets, market_identity, open_case
from tests.riskrequest.conftest import fresh_snapshot


async def _case(risk_db, now, trace, *, created_ago=timedelta(hours=2), atlas=True):
    _, sessions = risk_db
    cases = TradeCaseService(sessions, clock=FixedClock(now))
    trade_case = await open_early_case(cases, sessions, now, trace)
    if atlas:
        await record_early_onchain(cases, trade_case, now, created_ago=created_ago)
    return cases, trade_case


async def test_a_young_watch_with_too_short_history_gets_an_early_setup(risk_db, now, trace):
    cases, trade_case = await _case(risk_db, now, trace)
    result, envelope = await early_setup(
        cases, trade_case, RecordedMarkets(fresh_snapshot(now)), now, young_history(now, 3)
    )
    assert result.kind == "evidence", result
    record = envelope.payload.setup.early_entry
    assert record.vector_sufficiency == "MARKET_HISTORY_TOO_SHORT"
    assert record.young_history_sufficiency == "SUFFICIENT"
    assert record.closed_bars == 3
    assert record.strategy_policy_id == "PRE_VECTOR_EARLY_ENTRY_V1"
    assert record.workflow_version == "trade-case-early-v1"
    assert record.candidate_age_seconds == 2 * 3600
    assert record.max_age_seconds == 6 * 3600
    assert record.creation_timestamp == now - timedelta(hours=2)
    assert record.history_timeframe == "hour" and record.history_requested_bars == 48


async def test_an_empty_history_is_eligible(risk_db, now, trace):
    cases, trade_case = await _case(risk_db, now, trace)
    result, envelope = await early_setup(
        cases, trade_case, RecordedMarkets(fresh_snapshot(now)), now, young_history(now, 0)
    )
    assert result.kind == "evidence", result
    assert envelope.payload.setup.early_entry.vector_sufficiency == "MARKET_HISTORY_EMPTY"
    assert envelope.payload.setup.early_entry.young_history_sufficiency is None


async def test_sufficient_history_belongs_to_the_normal_path(risk_db, now, trace):
    cases, trade_case = await _case(risk_db, now, trace)
    result, _ = await early_setup(
        cases, trade_case, RecordedMarkets(fresh_snapshot(now)), now, young_history(now, 30)
    )
    assert result.kind == "failure"
    assert result.reason_code == "EARLY_VECTOR_HISTORY_SUFFICIENT"
    assert result.category.value == "CAPABILITY_DENIED"


@pytest.mark.parametrize(
    ("history", "reason"),
    [
        # A young series that stopped arriving four hours ago.
        (
            lambda now: young_history(now, 3, newest_close=now - timedelta(hours=4)),
            "MARKET_HISTORY_TOO_STALE",
        ),
        # Mostly gaps: two of five intervals with no trade (40 % > 25 %).
        (lambda now: young_history(now, 5, skip=frozenset({1, 2})), "MARKET_HISTORY_TOO_GAPPED"),
        (
            lambda now: young_history(now, 3, timeframe="minute"),
            "MARKET_HISTORY_TIMEFRAME_MISMATCH",
        ),
        (
            lambda now: young_history(now, 3).model_copy(update={"pair_id": "robinhood:x:other"}),
            "MARKET_HISTORY_IDENTITY_MISMATCH",
        ),
        (
            lambda now: young_history(now, 3).model_copy(update={"price_basis": "QUOTE_PER_BASE"}),
            "MARKET_HISTORY_PRICE_BASIS_MISMATCH",
        ),
        (
            lambda now: young_history(now, 3, newest_close=now + timedelta(hours=2)),
            "MARKET_HISTORY_IN_FUTURE",
        ),
    ],
)
async def test_broken_stale_or_contradictory_history_is_refused(
    risk_db, now, trace, history, reason
):
    cases, trade_case = await _case(risk_db, now, trace)
    result, envelope = await early_setup(
        cases, trade_case, RecordedMarkets(fresh_snapshot(now)), now, history(now)
    )
    assert envelope is None
    assert result.kind == "failure"
    assert result.reason_code == reason


async def test_the_age_boundary_is_inclusive_at_six_hours(risk_db, now, trace):
    cases, trade_case = await _case(risk_db, now, trace, created_ago=timedelta(hours=6))
    result, _ = await early_setup(
        cases, trade_case, RecordedMarkets(fresh_snapshot(now)), now, young_history(now, 3)
    )
    assert result.kind == "evidence", result


async def test_older_than_six_hours_is_refused(risk_db, now, trace):
    cases, trade_case = await _case(risk_db, now, trace, created_ago=timedelta(hours=6, seconds=1))
    result, _ = await early_setup(
        cases, trade_case, RecordedMarkets(fresh_snapshot(now)), now, young_history(now, 3)
    )
    assert result.kind == "failure"
    assert result.reason_code == "EARLY_CANDIDATE_TOO_OLD"


async def test_a_missing_creation_time_fails_closed(risk_db, now, trace):
    cases, trade_case = await _case(risk_db, now, trace, created_ago=None)
    result, _ = await early_setup(
        cases, trade_case, RecordedMarkets(fresh_snapshot(now)), now, young_history(now, 3)
    )
    assert result.kind == "failure"
    assert result.reason_code == "EARLY_CREATION_TIME_UNAVAILABLE"


async def test_the_producer_waits_for_atlas(risk_db, now, trace):
    cases, trade_case = await _case(risk_db, now, trace, atlas=False)
    result, _ = await early_setup(
        cases, trade_case, RecordedMarkets(fresh_snapshot(now)), now, young_history(now, 3)
    )
    assert result.kind == "wait"
    assert result.reason_code == "ATLAS_EVIDENCE_PENDING"


async def test_the_producer_waits_for_a_fresh_price(risk_db, now, trace):
    cases, trade_case = await _case(risk_db, now, trace)
    stale = fresh_snapshot(now, age=timedelta(minutes=6), metadata_age=timedelta(minutes=6))
    result, _ = await early_setup(
        cases, trade_case, RecordedMarkets(stale), now, young_history(now)
    )
    assert result.kind == "wait"
    assert result.reason_code == "MARKET_OBSERVATION_PENDING"


async def test_a_normal_case_is_never_treated_as_early(risk_db, now, trace):
    _, sessions = risk_db
    cases = TradeCaseService(sessions, clock=FixedClock(now))
    normal = await open_case(cases, now, trace, key="normal-case")
    assert normal.workflow_version == "trade-case-v2" and normal.strategy_policy_id is None
    result, _ = await early_setup(
        cases, normal, RecordedMarkets(fresh_snapshot(now)), now, young_history(now, 3)
    )
    assert result.kind == "failure"
    assert result.reason_code == "EARLY_STRATEGY_MISMATCH"


async def test_an_early_setup_cannot_be_filed_on_a_normal_case(risk_db, now, trace):
    """The workflow, not the producer, decides who may file a setup on a V2 case."""
    from src.orchestration.workflow.models import WorkflowFailure

    _, sessions = risk_db
    cases = TradeCaseService(sessions, clock=FixedClock(now))
    early = await open_early_case(cases, sessions, now, trace, key="early-src")
    await record_early_onchain(cases, early, now)
    result, _ = await early_setup(
        cases, early, RecordedMarkets(fresh_snapshot(now)), now, young_history(now, 3)
    )
    normal = await open_case(
        cases, now, trace, key="normal-target", identity=market_identity(network="testnet")
    )
    with pytest.raises(WorkflowFailure):
        await cases.record_evidence(normal.id, result.submission)
