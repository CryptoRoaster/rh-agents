"""Can the exact model input be reconstructed from the durable record alone?

A SHA-256 digest proves two inputs are equal. It cannot say what either one was.
If the provider revises a candle, changes its normalization or is replaced — or
if our own normalization changes — a digest alone leaves the question "what exact
market structure caused this setup?" unanswerable, and the standing invariant is
that decisions are traceable.

So the bounded structure is kept with the decision rather than referenced from
it. These tests prove the record is faithful, self-verifying and reconstructable,
and that it never claims data the model did not receive.
"""

import json
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.agents.vector.context import (
    setup_document,
    structure_digest,
    structure_document,
    vector_input_digest,
)
from src.agents.vector.models import ObservedBar, VectorMarketStructure
from src.orchestration.worker.models import EvidenceTaskResult
from src.orchestration.workflow.models import (
    EvidenceSubmission,
    RecordedMarketStructure,
    TradeSetupPayload,
)
from src.reasoning.fake import DeterministicReasoningProvider
from tests.vector.conftest import history_for, task_input
from tests.vector.test_scenarios import reply, run


def rebuild(record: RecordedMarketStructure) -> VectorMarketStructure:
    """The durable record, read back as the view the model was shown.

    Deliberately a plain field-for-field reconstruction with no defaults filled
    in: if the record were missing something the model saw, this would not
    produce an equal structure, and the digest below would not match.
    """
    return VectorMarketStructure(
        provider=record.provider,
        timeframe=record.timeframe,
        interval_seconds=record.interval_seconds,
        price_basis=record.price_basis,  # type: ignore[arg-type]
        bars=tuple(
            ObservedBar(
                opened_at=bar.opened_at,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
            for bar in record.bars
        ),
        coverage=record.coverage,
        requested_bars=record.requested_bars,
        missing_intervals=record.missing_intervals,
        window_start=record.window_start,
        window_end=record.window_end,
        # Age is relative to the moment of reading and is deliberately not part
        # of the record, so reconstruction supplies nothing for it.
        age_seconds=0,
    )


async def accepted(now, **overrides):
    context, outcome = await run(
        now, DeterministicReasoningProvider.returning(reply(now)), **overrides
    )
    assert isinstance(outcome, EvidenceTaskResult)
    return context, outcome.submission


# ------------------------------------------------------- the round trip


async def test_the_recorded_structure_reproduces_the_model_input_exactly(now):
    """The headline. Evidence in, canonical model input out, digests equal."""
    context, submission = await accepted(now)
    record = submission.payload.setup.structure
    assert record is not None

    reconstructed = rebuild(record)
    live = context.market.structure
    assert structure_document(reconstructed) == structure_document(live)
    assert structure_digest(reconstructed) == structure_digest(live)
    assert record.structure_digest == structure_digest(live)


async def test_the_round_trip_survives_serialization(now):
    """Through JSON and back, as a later reader would actually receive it."""
    context, submission = await accepted(now)
    revived = EvidenceSubmission.model_validate_json(submission.model_dump_json())
    record = revived.payload.setup.structure
    assert record is not None
    assert structure_digest(rebuild(record)) == record.structure_digest
    assert structure_digest(rebuild(record)) == structure_digest(context.market.structure)


async def test_every_bar_the_model_saw_is_in_the_record(now):
    """Not a summary of the window. The bars themselves."""
    context, submission = await accepted(now)
    record = submission.payload.setup.structure
    assert record is not None
    shown = context.market.structure.bars
    assert len(record.bars) == len(shown)
    for stored, seen in zip(record.bars, shown, strict=True):
        assert (stored.opened_at, stored.open, stored.high, stored.low, stored.close) == (
            seen.opened_at,
            seen.open,
            seen.high,
            seen.low,
            seen.close,
        )


async def test_the_record_names_the_market_it_belongs_to(now):
    context, submission = await accepted(now)
    record = submission.payload.setup.structure
    market = context.market
    assert (record.pair_id, record.chain, record.network, record.venue) == (
        market.pair_id,
        market.chain,
        market.network,
        market.venue,
    )
    assert (record.base_asset_id, record.quote_asset_id) == (
        market.base_asset_id,
        market.quote_asset_id,
    )
    assert record.policy_version == context.policy_version


async def test_a_tampered_record_fails_its_own_digest(now):
    """Self-verifying: the stored digest is recomputable from the stored bars."""
    _, submission = await accepted(now)
    record = submission.payload.setup.structure
    assert record is not None
    moved = record.model_copy(
        update={
            "bars": (
                *record.bars[:-1],
                record.bars[-1].model_copy(update={"high": record.bars[-1].high * Decimal(2)}),
            )
        }
    )
    assert structure_digest(rebuild(moved)) != moved.structure_digest


# ------------------------------------------- no hidden facts, either way


async def test_the_record_claims_nothing_the_model_was_not_given(now):
    """A durable audit snapshot must not be more generous than the input."""
    context, submission = await accepted(now)
    record = submission.payload.setup.structure
    shown = {bar.opened_at for bar in context.market.structure.bars}
    assert {bar.opened_at for bar in record.bars} == shown
    assert record.window_start == context.market.structure.window_start
    assert record.window_end == context.market.structure.window_end
    assert record.missing_intervals == context.market.structure.missing_intervals
    assert record.coverage == context.market.structure.coverage


async def test_the_model_received_nothing_the_record_omits(now):
    """And the reasoning input carries no market structure beyond the record."""
    context, submission = await accepted(now)
    record = submission.payload.setup.structure
    from src.agents.vector.context import reasoning_payload

    shown = reasoning_payload(context)["market_context"]["market_structure"]
    assert shown == structure_document(rebuild(record))


async def test_the_record_holds_no_provider_payload_or_transport_detail(now):
    """Bounded normalized facts only: no raw JSON, no headers, no latency."""
    _, submission = await accepted(now)
    rendered = json.dumps(json.loads(submission.model_dump_json())["payload"]["setup"]["structure"])
    for forbidden in (
        "http",
        "api_key",
        "Authorization",
        "latency",
        "fetched",
        "retrieved",
        "status_code",
        "ohlcv_list",
        "raw",
    ):
        assert forbidden not in rendered


# --------------------------------------------- retrieval time stays out


async def test_two_fetches_of_the_same_bars_record_the_same_thing(now):
    """Retrieval time is not market truth and is absent by construction."""
    series = history_for(now, bars=30)
    later = series.model_copy(update={"fetched_at": series.fetched_at + timedelta(hours=2)})
    first = task_input(now, history=series)
    again = task_input(now, history=later)
    assert series.fetched_at != later.fetched_at
    assert structure_digest(first.market.structure) == structure_digest(again.market.structure)
    assert vector_input_digest(first) == vector_input_digest(again)


async def test_source_bar_timestamps_remain_part_of_the_record(now):
    """Fetch time is out; the market's own account of when it traded is in."""
    _, submission = await accepted(now)
    record = submission.payload.setup.structure
    document = structure_document(rebuild(record))
    assert all("opened_at" in bar for bar in document["bars"])
    assert document["window_start"] and document["window_end"]


# --------------------------------------------------- one canonicalization


async def test_the_stored_form_and_the_hashed_form_are_the_same_form(now):
    """There is exactly one canonicalization, so the two cannot drift apart."""
    context, submission = await accepted(now)
    record = submission.payload.setup.structure
    inside_input = setup_document(context)["market_structure"]
    assert inside_input == structure_document(rebuild(record))


# ------------------------------------------------------- compatibility


def test_evidence_without_a_recorded_structure_still_parses():
    """Phase 2A payloads predate all of this and must stay readable."""
    payload = TradeSetupPayload.model_validate(
        {
            "kind": "trade_setup",
            "setup_id": str(uuid4()),
            "side": "BUY",
            "entry_price": "1.10",
            "invalidation_price": "0.92",
            "target_prices": ["1.25"],
        }
    )
    assert payload.setup is None
    assert TradeSetupPayload.model_validate_json(payload.model_dump_json()) == payload


def test_a_setup_detail_written_before_the_structure_field_still_parses():
    """Additive and optional: an earlier Phase 2H detail replays unchanged."""
    detail = {
        "setup_fingerprint": "a" * 64,
        "policy_version": "vector-setup-v2",
        "kind": "BREAKOUT_LONG",
        "price_basis": "USD_PER_BASE_UNIT",
        "entry_low": "1.10",
        "entry_high": "1.10",
        "reference_price": "1.00",
        "expires_at": "2026-09-09T14:00:00Z",
        "trigger": {
            "type": "PRICE_GTE",
            "price_basis": "USD_PER_BASE_UNIT",
            "reference_price": "1.10",
            "valid_from": "2026-09-09T12:00:00Z",
            "expires_at": "2026-09-09T14:00:00Z",
        },
        "reason_codes": ["PRICE_AVAILABLE"],
        "summary": "ok",
        "input_digest": "b" * 64,
    }
    payload = TradeSetupPayload.model_validate(
        {
            "kind": "trade_setup",
            "setup_id": str(uuid4()),
            "side": "BUY",
            "entry_price": "1.10",
            "invalidation_price": "0.92",
            "target_prices": ["1.15"],
            "setup": detail,
        }
    )
    assert payload.setup is not None
    assert payload.setup.structure is None
    assert payload.acceptance().value == "ACCEPTED"


def test_a_record_cannot_be_unbounded():
    """One decision's input, never an archive."""
    assert RecordedMarketStructure.model_fields["bars"].metadata
    limits = repr(RecordedMarketStructure.model_fields["bars"].metadata)
    assert "200" in limits


@pytest.mark.parametrize(
    "forbidden", ["fetched_at", "retrieved_at", "latency_ms", "raw_payload", "request_headers"]
)
def test_the_record_has_no_field_for_transport_noise(forbidden):
    assert forbidden not in RecordedMarketStructure.model_fields
