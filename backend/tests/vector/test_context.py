"""Assembly, freshness and the two fingerprints.

A setup is a statement about price levels, so the context layer's job is to
refuse to produce one when there is no current price to state them against — and
to do that *before* a model is asked, because asking anyway could only produce an
invented level that would have to be caught later, having already been paid for.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.agents.vector.context import (
    READABLE_EVIDENCE,
    VectorContextReader,
    build_setup,
    evidence_summaries,
    reasoning_payload,
    setup_document,
    setup_fingerprint,
    vector_input_digest,
)
from src.agents.vector.policy import VECTOR_SETUP_V1
from src.agents.vector.ports import VectorContextUnavailable
from src.agents.vector.validation import trigger_for
from src.core.clock import FixedClock
from src.core.models import AgentRole
from src.markets.fake import fixture_snapshot
from src.markets.models import Availability
from src.orchestration.workflow.models import (
    EvidenceEnvelope,
    EvidenceProvenance,
    EvidenceStatus,
    EvidenceType,
    SentimentPayload,
    TriggerPayload,
)
from tests.vector.conftest import (
    HISTORY_BARS,
    PAIR_ID,
    StubCases,
    StubHistory,
    StubMarkets,
    StubTradeCase,
    breakout,
    history_for,
    market_identity,
    task_input,
)


def snapshot_for(now, *, minutes_ago: int = 1, price=Decimal("1.00"), pair_id=PAIR_ID):
    """A recorded snapshot shaped like the market layer actually stores them."""
    base = fixture_snapshot(now - timedelta(minutes=minutes_ago), uuid4())
    pair = base.pair.model_copy(update={"pair_id": pair_id})
    price_snapshot = base.price.model_copy(
        update={
            "status": Availability.AVAILABLE if price is not None else Availability.UNKNOWN,
            "value_usd": price,
        }
    )
    return base.model_copy(update={"pair": pair, "price": price_snapshot})


# Distinguishes "no snapshot argument given" from "deliberately no snapshot".
DEFAULT = object()


async def read(now, *, snapshot=DEFAULT, evidence=(), market=None, history=DEFAULT):
    reader = VectorContextReader(
        cases=StubCases(StubTradeCase(market or market_identity()), evidence),
        markets=StubMarkets(snapshot_for(now) if snapshot is DEFAULT else snapshot),
        history=StubHistory(history_for(now) if history is DEFAULT else history),
        clock=FixedClock(now),
        include_fixtures=True,
    )
    return await reader.setup_context(uuid4(), uuid4())


# --------------------------------------------------- refusing to proceed


async def test_a_missing_market_observation_stops_before_the_model(now):
    with pytest.raises(VectorContextUnavailable) as error:
        await read(now, snapshot=None)
    assert error.value.reason_code == "MARKET_OBSERVATION_MISSING"


async def test_a_stale_observation_stops_before_the_model(now):
    """Scenario E. A fifteen-minute-old price is a different price."""
    with pytest.raises(VectorContextUnavailable) as error:
        await read(now, snapshot=snapshot_for(now, minutes_ago=15))
    assert error.value.reason_code == "MARKET_OBSERVATION_TOO_STALE"


async def test_an_unknown_price_stops_before_the_model(now):
    """Scenario F. No level to reason from — not a zero, not a guess."""
    with pytest.raises(VectorContextUnavailable) as error:
        await read(now, snapshot=snapshot_for(now, price=None))
    assert error.value.reason_code == "PRICE_UNAVAILABLE"


async def test_an_observation_for_another_market_is_refused(now):
    with pytest.raises(VectorContextUnavailable) as error:
        await read(now, snapshot=snapshot_for(now, pair_id="robinhood:mainnet:other"))
    assert error.value.reason_code == "MARKET_IDENTITY_MISMATCH"


async def test_an_observation_from_the_future_is_refused(now):
    with pytest.raises(VectorContextUnavailable) as error:
        await read(now, snapshot=snapshot_for(now, minutes_ago=-5))
    assert error.value.reason_code == "MARKET_OBSERVATION_IN_FUTURE"


async def test_a_fresh_priced_market_is_admitted(now):
    context = await read(now)
    assert context.market.pair_id == PAIR_ID
    assert context.latest_price == Decimal("1.00")
    assert context.policy_version == VECTOR_SETUP_V1.version


async def test_the_freshness_policy_is_vectors_own(now):
    """Tighter than discovery's, because this proposes levels rather than interest."""
    assert VECTOR_SETUP_V1.max_input_age == timedelta(minutes=5)
    await read(now, snapshot=snapshot_for(now, minutes_ago=4))
    with pytest.raises(VectorContextUnavailable):
        await read(now, snapshot=snapshot_for(now, minutes_ago=6))


# ------------------------------------------------------- price orientation


async def test_every_level_shares_one_declared_price_unit(now):
    """Scenario R. USD per base unit is the only orientation the market layer records."""
    context = await read(now)
    assert context.market.price_basis == "USD_PER_BASE_UNIT"
    assert context.market.base_asset_id.endswith("a1" * 20)
    assert context.market.quote_asset_id.endswith("b2" * 20)
    assert context.market.base_asset_id != context.market.quote_asset_id

    setup = build_setup(breakout(now), trigger_for(breakout(now), context), context, "0" * 64)
    assert setup.price_basis == "USD_PER_BASE_UNIT"
    assert setup.trigger.price_basis == "USD_PER_BASE_UNIT"
    # A reciprocal would put the levels below one; they are dollars per token.
    assert setup.entry_high > setup.reference_price


async def test_the_document_states_the_orientation_to_the_model(now):
    document = setup_document(await read(now))
    assert document["price_basis"] == "USD_PER_BASE_UNIT"


# -------------------------------------------------------------- digests


async def test_the_same_market_fingerprints_identically(now):
    snapshot, history = snapshot_for(now), history_for(now)
    first = await read(now, snapshot=snapshot, history=history)
    later = await read(now + timedelta(minutes=1), snapshot=snapshot, history=history)
    assert vector_input_digest(first) == vector_input_digest(later)


async def test_a_changed_price_changes_the_fingerprint(now):
    first = await read(now, snapshot=snapshot_for(now))
    moved = await read(now, snapshot=snapshot_for(now, price=Decimal("1.20")))
    assert vector_input_digest(first) != vector_input_digest(moved)


async def test_the_digest_ignores_when_we_read(now):
    """Observation age is relative to the read and is deliberately excluded."""
    snapshot, history = snapshot_for(now, minutes_ago=1), history_for(now)
    first = await read(now, snapshot=snapshot, history=history)
    later = await read(now + timedelta(minutes=2), snapshot=snapshot, history=history)
    assert first.market.age_seconds != later.market.age_seconds
    assert first.market.structure.age_seconds != later.market.structure.age_seconds
    assert vector_input_digest(first) == vector_input_digest(later)


# -------------------------------------------------- the setup fingerprint


async def test_the_same_proposal_over_the_same_input_is_one_setup(now):
    context = await read(now)
    digest = vector_input_digest(context)
    first = build_setup(breakout(now), trigger_for(breakout(now), context), context, digest)
    again = build_setup(breakout(now), trigger_for(breakout(now), context), context, digest)
    assert first.setup_fingerprint == again.setup_fingerprint
    assert first.setup_id == again.setup_id


@pytest.mark.parametrize(
    "change",
    [
        {"entry_low": Decimal("1.11"), "entry_high": Decimal("1.11")},
        {"invalidation_price": Decimal("0.90")},
        {"targets": (Decimal("1.30"),)},
        {"expires_at": None},
    ],
)
async def test_a_moved_level_is_a_different_setup(now, change):
    """PULSE references a specific setup, so "the same setup" must be decidable."""
    context = await read(now)
    digest = vector_input_digest(context)
    if change.get("expires_at", "keep") is None:
        change = {"expires_at": now + timedelta(hours=3)}
    base = breakout(now)
    moved = breakout(now, **change)
    assert setup_fingerprint(base, trigger_for(base, context), context, digest) != (
        setup_fingerprint(moved, trigger_for(moved, context), context, digest)
    )


async def test_a_different_input_makes_a_different_setup(now):
    """Identical geometry drawn from a different market is not the same proposal."""
    context = await read(now)
    proposal = breakout(now)
    trigger = trigger_for(proposal, context)
    assert setup_fingerprint(proposal, trigger, context, "0" * 64) != (
        setup_fingerprint(proposal, trigger, context, "1" * 64)
    )


async def test_the_setup_records_the_price_it_was_drawn_from(now):
    context = await read(now)
    setup = build_setup(breakout(now), trigger_for(breakout(now), context), context, "0" * 64)
    assert setup.reference_price == Decimal("1.00")
    assert setup.input_digest == "0" * 64


# ------------------------------------------------------- what the model sees


async def test_the_payload_quotes_data_and_carries_no_instructions(now):
    payload = reasoning_payload(await read(now))
    assert set(payload) == {"market_context"}
    document = payload["market_context"]
    assert isinstance(document, dict)
    assert "setup_horizon" in document
    assert document["setup_horizon"] == {"minimum_seconds": 300, "maximum_seconds": 14400}


async def test_an_unknown_measurement_reaches_the_model_as_unknown(now):
    """Never as a zero, which would be arithmetic on a fiction."""
    snapshot = snapshot_for(now)
    liquidity = snapshot.liquidity.model_copy(
        update={"status": Availability.UNKNOWN, "value_usd": None}
    )
    context = await read(now, snapshot=snapshot.model_copy(update={"liquidity": liquidity}))
    document = setup_document(context)
    entry = document["liquidity"]
    assert isinstance(entry, dict)
    assert entry["status"] == "UNKNOWN"
    assert entry["value_usd"] is None


async def test_the_document_carries_the_bars_the_levels_must_answer_to(now):
    """The correction to this phase's original defect, stated as a document.

    Before market history this document held one price and the model returned
    four numbers. Now the structure a level must answer to is in front of it, and
    the input digest fingerprints exactly those bars.
    """
    structure = setup_document(await read(now))["market_structure"]
    assert isinstance(structure, dict)
    assert len(structure["bars"]) == HISTORY_BARS
    assert structure["price_basis"] == "USD_PER_BASE_UNIT"
    assert structure["timeframe"] == "hour"
    assert structure["interval_seconds"] == 3600
    first = structure["bars"][0]
    assert set(first) == {"opened_at", "open", "high", "low", "close", "volume"}
    # Canonical decimal text, never a float and never through one.
    assert all(isinstance(first[key], str) for key in ("open", "high", "low", "close"))


async def test_no_indicator_or_derived_signal_is_computed_for_the_model(now):
    """Facts, not conclusions. VECTOR is given bars; it is not given a thesis.

    A computed trend or oscillator would be this system taking a view and then
    asking a model to agree with it, and afterwards the reasoning would be
    attributable to neither.
    """
    document = setup_document(await read(now))
    structure = document["market_structure"]
    assert isinstance(structure, dict)
    # Field names, not a rendered blob: "rsi" is a substring of "version", and a
    # substring search would have quietly passed for the wrong reason.
    names = set(document) | set(structure) | set(structure["bars"][0])
    for absent in ("rsi", "macd", "bollinger", "sma", "ema", "momentum", "moving_average"):
        assert absent not in names


async def test_the_current_price_and_the_newest_close_stay_separate_facts(now):
    """A careless assembler would collapse these into one number."""
    context = await read(now)
    newest_close = context.market.structure.bars[-1].close
    assert context.latest_price == Decimal("1.00")
    assert newest_close != context.latest_price
    # The snapshot keeps its own observation time; the series keeps the moment
    # its last bar closed. Neither is derived from the other.
    assert context.market.observed_at != context.market.structure.window_end


def test_the_task_input_has_no_field_through_which_a_capability_could_arrive():
    from src.agents.vector.models import VectorTaskInput

    for forbidden in ("session", "client", "markets", "provider_client", "rpc", "submit"):
        assert forbidden not in VectorTaskInput.model_fields


async def test_the_assembled_input_carries_no_client_session_or_url(now):
    rendered = (await read(now)).model_dump_json()
    for forbidden in ("http://", "https://", "postgresql", "api_key", "Authorization"):
        assert forbidden not in rendered


def test_a_task_input_exposes_its_own_observation_identities(now):
    context = task_input(now)
    assert context.market.snapshot_id in context.market.observation_ids
    assert len(context.market.observation_ids) == 4


# ------------------------------------------ what other roles contribute


def evidence_envelope(now, evidence_type, role, payload, *, status=EvidenceStatus.AVAILABLE):
    evidence_id = uuid4()
    return EvidenceEnvelope(
        evidence_id=evidence_id,
        trade_case_id=uuid4(),
        producer_role=role,
        evidence_type=evidence_type,
        provenance=EvidenceProvenance(source="test", reference_id=uuid4()),
        observed_at=now,
        created_at=now,
        recorded_at=now,
        valid_until=now + timedelta(minutes=30),
        status=status,
        reason_codes=() if status == EvidenceStatus.AVAILABLE else ("SOURCE_UNKNOWN",),
        payload=payload,
        correlation_id=uuid4(),
        idempotency_key=str(evidence_id),
        submission_fingerprint="b" * 64,
    )


def test_a_usable_finding_from_another_role_reaches_the_model(now):
    envelope = evidence_envelope(
        now, EvidenceType.SENTIMENT, AgentRole.SIGNAL, SentimentPayload(assessment="NEUTRAL")
    )
    summaries = evidence_summaries({EvidenceType.SENTIMENT: envelope}, now)
    assert [item.headline for item in summaries] == ["NEUTRAL"]


def test_an_unusable_finding_is_absent_rather_than_quietly_downgraded(now):
    """A blocked or stale conclusion is not context a setup may be built on."""
    stale = evidence_envelope(
        now, EvidenceType.SENTIMENT, AgentRole.SIGNAL, SentimentPayload(assessment="NEUTRAL")
    )
    assert evidence_summaries({EvidenceType.SENTIMENT: stale}, now + timedelta(hours=1)) == ()

    unknown = evidence_envelope(
        now,
        EvidenceType.SENTIMENT,
        AgentRole.SIGNAL,
        SentimentPayload(assessment="NEUTRAL"),
        status=EvidenceStatus.UNKNOWN,
    )
    assert evidence_summaries({EvidenceType.SENTIMENT: unknown}, now) == ()


def test_evidence_downstream_of_a_setup_is_never_offered_as_input_to_one(now):
    """Reading a trigger or an execution assessment here would be circular."""
    assert EvidenceType.TRIGGER not in READABLE_EVIDENCE
    assert EvidenceType.LIQUIDITY_EXECUTION not in READABLE_EVIDENCE
    trigger = evidence_envelope(
        now,
        EvidenceType.TRIGGER,
        AgentRole.PULSE,
        TriggerPayload(
            setup_evidence_id=uuid4(),
            observed_price=Decimal("1"),
            trigger_code="ENTRY_LEVEL_REACHED",
        ),
    )
    assert evidence_summaries({EvidenceType.TRIGGER: trigger}, now) == ()
