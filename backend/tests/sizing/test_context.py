"""The reader, against the real workflow service and real recorded observations.

Nothing is stubbed except the market feed's single read method, and that feed
returns snapshots the market layer itself produces rather than hand-built
objects. The case, its evidence, its supersession chain and its revisions all go
through `TradeCaseService` and a real database.

This is still not an autonomously running system. No worker runs, no launcher
exists, and the observations arrive from a fixture rather than a provider — a
fixture run proves the wiring, not the operation.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select

from src.core.models import TradingMode
from src.data.tables import TradeCaseRiskBindingRow
from src.orchestration.sizing.models import SizingRefusal
from src.orchestration.sizing.policy import PAPER_SIZING_V1
from tests.sizing.conftest import (
    BASE_ASSET,
    PAIR_ID,
    RecordedMarkets,
    build_reader,
    open_case,
    record_setup,
    recorded_snapshot,
)


async def prepared(sessions, now, trace, *, feed=None, **overrides):
    """An opened case with a current setup, and a reader pointed at it."""
    reader = build_reader(
        sessions, now, feed=feed if feed is not None else RecordedMarkets(recorded_snapshot(now))
    )
    trade_case = await open_case(reader.cases, now, trace)
    setup = await record_setup(reader.cases, trade_case, now)
    if overrides:
        reader = build_reader(sessions, now, feed=reader.markets, **overrides)
    return reader, trade_case, setup


async def test_a_prepared_case_gets_a_requested_size(worker_db, now, trace):
    """The whole phase, end to end over real state."""
    _, sessions = worker_db
    reader, trade_case, setup = await prepared(sessions, now, trace)

    reading = await reader.sizing(trade_case.id)

    assert reading.kind == "sizing_assessment"
    assert reading.trade_case_id == trade_case.id
    assert reading.base_asset_id == BASE_ASSET
    assert reading.setup_evidence_id == setup.evidence_id
    assert reading.requested_notional_usd == Decimal("500")
    assert reading.quantity * reading.reference_price.usd_per_base_unit <= Decimal("500")
    assert reading.policy_version == "paper-sizing-v1"


async def test_the_reader_asks_only_about_this_case_s_own_market(worker_db, now, trace):
    """The market is the case's, never a parameter a caller could choose."""
    _, sessions = worker_db
    reader, trade_case, _ = await prepared(sessions, now, trace)

    await reader.sizing(trade_case.id)

    assert reader.markets.requested == [(PAIR_ID, True)]


async def test_an_unknown_case_is_refused_without_a_market_read(worker_db, now, trace):
    _, sessions = worker_db
    reader, _, _ = await prepared(sessions, now, trace)

    reading = await reader.sizing(uuid4())

    assert reading.reason == SizingRefusal.SIZING_CASE_UNAVAILABLE
    assert reading.base_asset_id is None
    assert reader.markets.requested == []


async def test_a_case_without_a_current_setup_has_no_entry_to_size(worker_db, now, trace):
    _, sessions = worker_db
    reader = build_reader(sessions, now, feed=RecordedMarkets(recorded_snapshot(now)))
    trade_case = await open_case(reader.cases, now, trace)

    reading = await reader.sizing(trade_case.id)

    assert reading.reason == SizingRefusal.SIZING_NO_CURRENT_SETUP


async def test_an_unrecorded_market_is_refused(worker_db, now, trace):
    _, sessions = worker_db
    reader, trade_case, _ = await prepared(sessions, now, trace, feed=RecordedMarkets(None))

    reading = await reader.sizing(trade_case.id)

    assert reading.reason == SizingRefusal.SIZING_MARKET_NOT_RECORDED


async def test_a_snapshot_without_a_usable_price_is_refused(worker_db, now, trace):
    _, sessions = worker_db
    feed = RecordedMarkets(recorded_snapshot(now, price=None))
    reader, trade_case, _ = await prepared(sessions, now, trace, feed=feed)

    reading = await reader.sizing(trade_case.id)

    assert reading.reason == SizingRefusal.SIZING_PRICE_UNAVAILABLE


async def test_a_snapshot_without_recorded_decimals_is_refused(worker_db, now, trace):
    """The recorded pair is the only trusted metadata source there is.

    ATLAS does read ERC-20 `decimals()` on chain, but that figure never reaches
    durable evidence, so it cannot be read back here. Absent decimals therefore
    mean absent, and eighteen is not assumed in their place.
    """
    _, sessions = worker_db
    feed = RecordedMarkets(recorded_snapshot(now, decimals=None))
    reader, trade_case, _ = await prepared(sessions, now, trace, feed=feed)

    reading = await reader.sizing(trade_case.id)

    assert reading.reason == SizingRefusal.SIZING_TOKEN_METADATA_MISSING


async def test_an_aged_out_observation_is_refused(worker_db, now, trace):
    _, sessions = worker_db
    stale = recorded_snapshot(now, age=PAPER_SIZING_V1.max_price_age + timedelta(seconds=1))
    reader, trade_case, _ = await prepared(sessions, now, trace, feed=RecordedMarkets(stale))

    reading = await reader.sizing(trade_case.id)

    assert reading.reason == SizingRefusal.SIZING_PRICE_STALE


async def test_an_unconfigured_deployment_changes_nothing(worker_db, now, trace):
    """The default path, and the one that must stay exactly as it was."""
    _, sessions = worker_db
    reader, trade_case, _ = await prepared(sessions, now, trace, notional=None)

    reading = await reader.sizing(trade_case.id)

    assert reading.reason == SizingRefusal.AUTONOMOUS_SIZING_INPUT_MISSING


async def test_the_default_mode_refuses_to_size(worker_db, now, trace):
    """Fails closed. A deployment that has not said PAPER has not said anything."""
    _, sessions = worker_db
    feed = RecordedMarkets(recorded_snapshot(now))
    reader = build_reader(sessions, now, feed=feed)
    trade_case = await open_case(reader.cases, now, trace)
    await record_setup(reader.cases, trade_case, now)

    from src.orchestration.sizing.context import PaperSizingReader

    default = PaperSizingReader(
        cases=reader.cases,
        markets=feed,
        requested_notional_usd=Decimal("500"),
        clock=reader.clock,
        include_fixtures=True,
    )
    assert default.trading_mode is TradingMode.OBSERVE
    reading = await default.sizing(trade_case.id)
    assert reading.reason == SizingRefusal.SIZING_MODE_NOT_SUPPORTED


async def test_a_superseded_setup_is_a_different_reading(worker_db, now, trace):
    """Real supersession through the service, not a copied identifier.

    The quantity is unchanged because nothing the arithmetic reads changed. The
    identity is not, because the assessment is about this setup.
    """
    _, sessions = worker_db
    reader, trade_case, first_setup = await prepared(sessions, now, trace)
    before = await reader.sizing(trade_case.id)

    from src.core.models import AgentRole
    from src.orchestration.workflow.models import EvidenceType
    from tests.worker.conftest import setup_payload, submission

    replacement = await reader.cases.record_evidence(
        trade_case.id,
        submission(
            trade_case,
            now,
            AgentRole.VECTOR,
            EvidenceType.TRADE_SETUP,
            setup_payload(),
            key=f"sizing-setup-2-{trade_case.id}",
            supersedes_id=first_setup.evidence_id,
        ),
    )
    after = await reader.sizing(trade_case.id)

    assert after.setup_evidence_id == replacement.evidence_id != first_setup.evidence_id
    assert after.quantity == before.quantity
    assert after.input_digest != before.input_digest


async def test_a_successful_reading_writes_nothing(worker_db, now, trace):
    """A reading permits nothing and records nothing.

    No risk binding appears, the case does not move, and its evidence set is
    untouched. Whatever else sizing is, it is not a step in the workflow.
    """
    engine, sessions = worker_db
    reader, trade_case, _ = await prepared(sessions, now, trace)
    before = await reader.cases.get_trade_case(trade_case.id)
    envelopes = len(await reader.cases.evidence(trade_case.id))

    reading = await reader.sizing(trade_case.id)
    assert reading.kind == "sizing_assessment"

    after = await reader.cases.get_trade_case(trade_case.id)
    assert (after.status, after.revision) == (before.status, before.revision)
    assert len(await reader.cases.evidence(trade_case.id)) == envelopes
    async with sessions() as session:
        bindings = await session.scalar(select(func.count()).select_from(TradeCaseRiskBindingRow))
    assert bindings == 0
