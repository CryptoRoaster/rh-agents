"""Real provider observations through the unchanged Phase 2F pipeline.

The point of these tests is that nothing was made easier. Real casts go through
the same window validation, the same binding resolution, the same duplicate
clustering and author namespacing, the same quality policy and the same
validator as fixtures did. There is no "the provider already filtered this"
shortcut anywhere, because a provider that could pre-filter could pre-decide.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.agents.signal.context import SignalContextReader
from src.agents.signal.models import (
    MarketBindingBasis,
    QualitativeLevel,
    SignalDataQuality,
    SignalGap,
)
from src.agents.signal.sources.factory import social_source
from src.agents.signal.sources.neynar import NeynarSignalSource
from src.core.clock import FixedClock
from src.core.config import Settings
from src.orchestration.workflow.models import EvidenceStatus, EvidenceType, TradeCaseStatus
from src.reasoning.fake import DeterministicReasoningProvider
from tests.signal.conftest import TOKEN, StubCases, StubTradeCase, market_identity
from tests.signal.fake_http import RecordingRoutes, json_response, pages
from tests.signal.test_neynar import CONFIG, cast, page

NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)
DATABASE = "postgresql+asyncpg://user@localhost:5432/rh_agents"


def reader_for(handler, *, now=NOW) -> SignalContextReader:
    recording = RecordingRoutes(handler)
    return SignalContextReader(
        cases=StubCases(StubTradeCase(market_identity())),
        source=NeynarSignalSource(
            config=CONFIG,
            transport_factory=recording.transport_factory(),
            clock=FixedClock(now),
        ),
        clock=FixedClock(now),
    )


async def context_for(handler, *, now=NOW):
    return await reader_for(handler, now=now).sentiment_context(uuid4(), uuid4())


def conversation(count: int, *, chain_context: bool = True) -> list[dict[str, object]]:
    """Distinct authors saying distinct things, each naming the chain."""
    prefix = "Robinhood Chain" if chain_context else "just found"
    return [
        cast(
            index,
            text=f"{prefix} {TOKEN} — note number {index} about the settlement layer",
            fid=5000 + index,
            minutes_ago=10 + index * 3,
        )
        for index in range(count)
    ]


# ------------------------------------------------- real data, real pipeline


async def test_real_casts_reach_the_quality_layer_with_strong_bindings():
    """Scenario A, end to end."""
    task_input = await context_for(pages(page(conversation(12))))
    assert task_input.features.observation_count == 12
    assert task_input.features.unique_authoring_count == 12
    assert task_input.features.strong_binding_count == 12
    assert task_input.structure.data_quality == SignalDataQuality.USABLE
    assert all(item.author_key.startswith("FARCASTER:") for item in task_input.representatives)


async def test_unscoped_addresses_are_admitted_and_counted_as_weak():
    """Scenario B, end to end. Useful, and explicitly not proof of chain."""
    task_input = await context_for(pages(page(conversation(12, chain_context=False))))
    assert task_input.features.observation_count == 12
    assert task_input.features.strong_binding_count == 0
    assert task_input.features.weak_binding_count == 12
    # A set that rests entirely on weak bindings is never clean.
    assert task_input.structure.data_quality == SignalDataQuality.DEGRADED
    assert SignalGap.NO_STRONGLY_BOUND_OBSERVATIONS in task_input.structure.gaps
    assert all(
        item.binding_basis == MarketBindingBasis.CONTRACT_ADDRESS_UNSCOPED
        for item in task_input.representatives
    )


async def test_another_chains_context_is_excluded_by_the_quality_layer():
    """Scenario C. The adapter reports what the cast said; Phase 2F refuses it."""
    casts = [
        cast(index, text=f"BNB Smart Chain listing {TOKEN} #{index}", fid=6000 + index)
        for index in range(8)
    ]
    task_input = await context_for(pages(page(casts)))
    assert task_input.features.observation_count == 0
    assert task_input.features.excluded_unbound_count == 8
    assert task_input.structure.data_quality == SignalDataQuality.INSUFFICIENT


async def test_casts_that_only_mention_a_ticker_are_excluded():
    """Scenario D. A search result is not a binding."""
    casts = [cast(index, text=f"$DEMO pumping #{index}", fid=7000 + index) for index in range(8)]
    task_input = await context_for(pages(page(casts)))
    assert task_input.features.observation_count == 0
    assert task_input.features.excluded_unbound_count == 8


async def test_the_provider_cannot_widen_the_window():
    """Scenario H. The query was bounded and the answer is checked anyway."""
    stale = [
        cast(index, text=f"Robinhood Chain {TOKEN} #{index}", fid=8000 + index, minutes_ago=600)
        for index in range(8)
    ]
    task_input = await context_for(pages(page(stale)))
    assert task_input.features.observation_count == 0
    assert task_input.features.excluded_outside_window_count == 8
    assert SignalGap.ALL_OBSERVATIONS_OUTSIDE_WINDOW in task_input.structure.gaps


async def test_a_real_copy_campaign_keeps_all_of_its_evidence():
    """Scenario F. Distinct FIDs, one sentence. The provider changed nothing."""
    shared = f"🚀 Robinhood Chain {TOKEN} is the next 100x, buy now!"
    casts = [cast(index, text=shared, fid=9000 + index, minutes_ago=30) for index in range(40)]
    task_input = await context_for(pages(page(casts)))
    assert task_input.features.observation_count == 40
    # Forty accounts, one thing said.
    assert task_input.features.unique_authoring_count == 40
    assert task_input.features.unique_content_count == 1
    assert task_input.features.duplicate_share == 1
    assert task_input.structure.manipulation_concern >= QualitativeLevel.HIGH
    assert SignalGap.DUPLICATE_DOMINATED in task_input.structure.gaps


async def test_recast_counts_never_become_authors():
    """Scenario G. Five hundred amplifications, one voice."""
    casts = [
        cast(index, text=f"Robinhood Chain {TOKEN} #{index}", fid=1234, recasts=500)
        for index in range(6)
    ]
    task_input = await context_for(pages(page(casts)))
    assert task_input.features.observation_count == 6
    assert task_input.features.unique_authoring_count == 1
    assert task_input.features.repost_count == 0
    assert task_input.structure.organic_breadth == QualitativeLevel.VERY_LOW


async def test_a_provider_outage_is_an_explicit_absence(now=NOW):
    """Scenario J, at the collector."""
    recording = RecordingRoutes(lambda request: json_response({"error": "down"}, status=503))
    reader = SignalContextReader(
        cases=StubCases(StubTradeCase(market_identity())),
        source=NeynarSignalSource(
            config=CONFIG, transport_factory=recording.transport_factory(), clock=FixedClock(now)
        ),
        clock=FixedClock(now),
    )
    task_input = await reader.sentiment_context(uuid4(), uuid4())
    assert task_input.structure.data_quality == SignalDataQuality.INSUFFICIENT
    assert SignalGap.SOURCE_UNAVAILABLE in task_input.structure.gaps
    assert task_input.features.received_count == 0


async def test_the_digest_is_stable_across_identical_provider_answers():
    """Re-fetching the same casts is not evidence that anything moved."""
    from src.agents.signal.context import signal_input_digest

    first = await context_for(pages(page(conversation(12))))
    later = await context_for(pages(page(conversation(12))), now=NOW + timedelta(minutes=2))
    assert signal_input_digest(first) == signal_input_digest(later)


# ----------------------------------------------------- workflow behaviour


async def test_an_outage_leaves_a_required_prerequisite_unmet(worker_db, now, trace):
    """Scenario J, in the workflow. No fabricated neutral sentiment."""
    from tests.signal.test_workflow import build_stack, open_case, run_signal, sentiment_evidence

    _, sessions = worker_db
    recording = RecordingRoutes(lambda request: json_response({"error": "down"}, status=503))
    runtime, _ = build_stack(sessions, now)
    reader = SignalContextReader(
        cases=runtime.cases,
        source=NeynarSignalSource(
            config=CONFIG, transport_factory=recording.transport_factory(), clock=FixedClock(now)
        ),
        clock=FixedClock(now),
    )
    trade_case = await open_case(runtime.cases, now, trace, "neynar-outage")
    await run_signal(runtime, reader, "neynar-outage-worker")

    envelope = (await sentiment_evidence(runtime.cases, trade_case.id))[0]
    assert envelope.status == EvidenceStatus.UNAVAILABLE
    assert envelope.payload.assessment == "UNKNOWN"
    updated = await runtime.cases.get_trade_case(trade_case.id)
    assert updated.status == TradeCaseStatus.EVIDENCE_PENDING


async def test_the_case_progresses_once_the_provider_recovers(worker_db, now, trace):
    """Scenario K. No manual transition; the evaluator simply re-runs."""
    from tests.signal.test_workflow import build_stack, open_case, run_signal, sentiment_evidence

    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    outage = RecordingRoutes(lambda request: json_response({"error": "down"}, status=503))
    reader = SignalContextReader(
        cases=runtime.cases,
        source=NeynarSignalSource(
            config=CONFIG, transport_factory=outage.transport_factory(), clock=FixedClock(now)
        ),
        clock=FixedClock(now),
    )
    trade_case = await open_case(runtime.cases, now, trace, "neynar-recovery")
    await run_signal(runtime, reader, "neynar-recovery-worker")
    assert (await runtime.cases.get_trade_case(trade_case.id)).status == (
        TradeCaseStatus.EVIDENCE_PENDING
    )

    later = now + timedelta(minutes=5)
    healthy = RecordingRoutes(pages(page(_conversation_at(later, 12))))
    later_runtime, _ = build_stack(sessions, later)
    later_reader = SignalContextReader(
        cases=later_runtime.cases,
        source=NeynarSignalSource(
            config=CONFIG, transport_factory=healthy.transport_factory(), clock=FixedClock(later)
        ),
        clock=FixedClock(later),
    )
    refreshed = await later_reader.sentiment_context(trade_case.id, uuid4())
    assert refreshed.structure.data_quality == SignalDataQuality.USABLE
    assert refreshed.supersedes_evidence_id is not None

    from src.agents.signal.handler import SignalWorkerHandler
    from src.orchestration.worker.capabilities import SignalCapabilities
    from tests.signal.test_scenarios import Fixed, NoSubmit, lease_for, reply

    handler = SignalWorkerHandler(provider=DeterministicReasoningProvider.returning(reply()))
    # The runtime binds evidence to the case's correlation, so the lease carries it.
    lease = lease_for(refreshed, later).model_copy(update={"correlation_id": trace})
    report = await handler.handle(
        lease, SignalCapabilities(lease=lease, context=Fixed(refreshed), submit=NoSubmit())
    )
    await later_runtime.cases.record_evidence(trade_case.id, report.submission)
    envelopes = await sentiment_evidence(later_runtime.cases, trade_case.id)
    current = next(item for item in envelopes if item.supersedes_id is not None)
    assert current.status == EvidenceStatus.AVAILABLE


def _conversation_at(moment: datetime, count: int) -> list[dict[str, object]]:
    offset = int((moment - NOW).total_seconds() // 60)
    return [
        cast(
            index,
            text=f"Robinhood Chain {TOKEN} — note {index}",
            fid=5000 + index,
            minutes_ago=10 + index * 3 - offset,
        )
        for index in range(count)
    ]


async def test_sentiment_stays_out_of_the_risk_snapshot_with_real_data(worker_db, now, trace):
    """Scenario L. Real observations change nothing about that boundary."""
    from src.orchestration.workflow.engine import risk_input_digest
    from tests.signal.test_workflow import build_stack, open_case, run_signal

    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    healthy = RecordingRoutes(pages(page(_conversation_at(now, 12))))
    reader = SignalContextReader(
        cases=runtime.cases,
        source=NeynarSignalSource(
            config=CONFIG, transport_factory=healthy.transport_factory(), clock=FixedClock(now)
        ),
        clock=FixedClock(now),
    )
    trade_case = await open_case(runtime.cases, now, trace, "neynar-digest")
    await run_signal(runtime, reader, "neynar-digest-worker")

    evidence = await runtime.cases.evidence(trade_case.id)
    current = {item.evidence_type: item for item in evidence if item.supersedes_id is None}
    assert risk_input_digest(trade_case, current) == risk_input_digest(
        trade_case, {k: v for k, v in current.items() if k != EvidenceType.SENTIMENT}
    )


# ------------------------------------------------------------- the factory


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, database_url=DATABASE, **overrides)


def test_no_social_source_is_constructed_by_default():
    """A fresh deployment keeps Phase 2F's providerless behaviour exactly."""
    assert social_source(settings()) is None


def test_a_credential_alone_activates_nothing():
    from pydantic import SecretStr

    assert social_source(settings(neynar_api_key=SecretStr("neynar_x"))) is None


def test_selecting_the_provider_builds_exactly_that_source():
    from pydantic import SecretStr

    configured = settings(signal_social_provider="neynar", neynar_api_key=SecretStr("neynar_x"))
    built = social_source(configured)
    assert isinstance(built, NeynarSignalSource)
    assert built.config.base_url == "https://api.neynar.com"
    assert built.config.max_pages == 2


def test_a_selected_provider_without_its_key_refuses_to_boot():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        settings(signal_social_provider="neynar")


@pytest.mark.parametrize(
    "url",
    [
        "http://api.neynar.com",
        "https://api.neynar.com.evil.example",
        "https://user:secret@api.neynar.com",
        "https://api.neynar.com/?apikey=leaked",
        "https://api.neynar.com#fragment",
        "https://neynar.com",
    ],
)
def test_the_provider_origin_cannot_be_pointed_anywhere_else(url):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        settings(neynar_base_url=url)


def test_there_is_no_fake_provider_option():
    """A synthetic feed must not be reachable from a production configuration."""
    from typing import get_args

    annotation = Settings.model_fields["signal_social_provider"].annotation
    assert set(get_args(annotation)) == {"disabled", "neynar"}


def test_the_key_is_secret_and_never_renders():
    from pydantic import SecretStr

    configured = settings(neynar_api_key=SecretStr("neynar_supersecret"))
    assert "neynar_supersecret" not in repr(configured)
    assert "neynar_supersecret" not in configured.model_dump_json()
    assert configured.neynar_api_key.get_secret_value() == "neynar_supersecret"
