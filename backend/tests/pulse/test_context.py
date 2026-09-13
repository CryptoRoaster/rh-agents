"""Assembling the view: which setup is watched, and which price judges it.

The question this file answers is *authority*. A monitor that watched whichever
setup looked newest, or judged it against whatever price happened to be lying
around, would produce triggers nobody could act on — and the runtime would have
to catch them afterwards.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.agents.pulse.context import PulseContextReader, price_observation, watched_trigger
from src.agents.pulse.policy import PULSE_TRIGGER_V1
from src.core.clock import FixedClock
from src.core.models import Side
from src.markets.models import Availability
from src.orchestration.workflow.models import EvidenceStatus, TradeSetupPayload
from tests.pulse.conftest import (
    LEVEL,
    PAIR_ID,
    SPOT,
    StubCases,
    StubMarkets,
    StubTradeCase,
    market_identity,
    setup_envelope,
)
from tests.pulse.test_workflow import snapshot_for


async def read(now, *, evidence=(), snapshot="default", market=None):
    reader = PulseContextReader(
        cases=StubCases(StubTradeCase(market or market_identity()), evidence),
        markets=StubMarkets(snapshot_for(now) if snapshot == "default" else snapshot),
        clock=FixedClock(now),
        include_fixtures=True,
    )
    return await reader.trigger_context(uuid4(), uuid4())


# ------------------------------------------------- which setup is watched


async def test_the_authoritative_setup_supplies_the_condition(now):
    context = await read(now, evidence=(setup_envelope(now),))
    assert context.trigger is not None
    assert context.trigger.reference_price == LEVEL
    assert context.trigger.type.value == "PRICE_GTE"
    assert context.market_pair_id == PAIR_ID


async def test_a_superseded_setup_is_never_watched(now):
    """Selection goes through the workflow's own answer, not a heuristic."""
    old = setup_envelope(now, evidence_id=uuid4())
    new = setup_envelope(
        now, evidence_id=uuid4(), trigger={"reference_price": Decimal("9.00")}
    ).model_copy(update={"supersedes_id": old.evidence_id})
    context = await read(now, evidence=(old, new))
    assert context.trigger is not None
    assert context.trigger.reference_price == Decimal("9.00")


async def test_no_setup_at_all_leaves_nothing_to_watch(now):
    context = await read(now)
    assert context.trigger is None


async def test_an_unusable_setup_supplies_no_condition(now):
    """A setup the workflow considers unusable on its content is not watched."""
    unknown = setup_envelope(now, status=EvidenceStatus.UNKNOWN)
    assert watched_trigger(unknown, now) is None


async def test_an_expired_setup_still_surfaces_so_the_watch_can_end(now):
    """Expiry is the deliberate exception to the unusable rule.

    VECTOR ties an envelope's validity to the setup's own expiry, so at that
    instant the evidence is stale *and* the condition has run out. Hiding it
    would report "nothing to watch" — which is what a monitor says while waiting
    for a new setup — and the watch would recheck a window that has closed for
    good.
    """
    envelope = setup_envelope(now, valid_for=timedelta(hours=1))
    after = now + timedelta(hours=2)
    trigger = watched_trigger(envelope, after)
    assert trigger is not None
    assert trigger.expires_at < after


async def test_a_setup_without_a_machine_condition_is_not_guessed_at(now):
    """Pre-Phase-2H evidence has no trigger grammar, and none is inferred.

    Deriving a threshold from the legacy entry price would be inventing the
    contract rather than reading it.
    """
    legacy = TradeSetupPayload(
        setup_id=uuid4(),
        side=Side.BUY,
        entry_price=LEVEL,
        invalidation_price=Decimal("0.92"),
        target_prices=(Decimal("1.30"),),
    )
    assert legacy.setup is None
    assert watched_trigger(setup_envelope(now, payload=legacy), now) is None


def test_an_unknown_trigger_grammar_is_refused_rather_than_approximated(now):
    envelope = setup_envelope(now)
    detail = envelope.payload.setup.model_copy(
        update={"trigger": envelope.payload.setup.trigger.model_copy(update={"type": "PRICE_NEAR"})}
    )
    payload = envelope.payload.model_copy(update={"setup": detail})
    assert watched_trigger(envelope.model_copy(update={"payload": payload}), now) is None


async def test_the_condition_is_copied_rather_than_referenced(now):
    """So the evidence records what was watched even if the setup changes after."""
    envelope = setup_envelope(now)
    trigger = watched_trigger(envelope, now)
    assert trigger is not None
    assert trigger.setup_evidence_id == envelope.evidence_id
    assert trigger.setup_id == envelope.payload.setup_id
    assert trigger.setup_fingerprint == envelope.payload.setup.setup_fingerprint
    assert trigger.valid_from == envelope.payload.setup.trigger.valid_from
    assert trigger.expires_at == envelope.payload.setup.trigger.expires_at


# ------------------------------------------------------- which price


async def test_the_recorded_price_becomes_the_observation(now):
    context = await read(now, evidence=(setup_envelope(now),))
    assert len(context.observations) == 1
    assert context.observations[0].price == SPOT
    assert context.observations[0].price_basis == "USD_PER_BASE_UNIT"
    assert context.observations[0].pair_id == PAIR_ID


async def test_an_unknown_price_produces_no_observation(now):
    """Absent, never zero and never a previous value carried forward."""
    context = await read(
        now, evidence=(setup_envelope(now),), snapshot=snapshot_for(now, price=None)
    )
    assert context.observations == ()
    assert context.latest is None


async def test_no_market_data_at_all_produces_no_observation(now):
    context = await read(now, evidence=(setup_envelope(now),), snapshot=None)
    assert context.observations == ()
    assert context.latest is None


def test_a_non_positive_price_never_becomes_an_observation(now):
    """Belt and braces over the market layer's own rule."""
    snapshot = snapshot_for(now)
    zeroed = snapshot.price.model_copy(
        update={"status": Availability.AVAILABLE, "value_usd": Decimal("0")}
    )
    assert (
        price_observation(
            snapshot.model_copy(update={"price": zeroed}), StubTradeCase(market_identity())
        )
        is None
    )


async def test_a_price_from_another_pool_is_carried_through_to_be_refused(now):
    """Not silently dropped: a monitor that saw no price would wait in silence.

    The mismatch is passed to the evaluator so it becomes an explicit, typed
    refusal that somebody can read.
    """
    elsewhere = snapshot_for(now, pair_id="ethereum:mainnet:contract_address:0x" + "ff" * 20)
    context = await read(now, evidence=(setup_envelope(now),), snapshot=elsewhere)
    assert context.observations
    assert context.observations[0].pair_id != context.market_pair_id


# --------------------------------------------------------- the surface


async def test_the_view_contains_nothing_it_does_not_need(now):
    """No candles, no rationale, no other roles' conclusions, no history."""
    from src.agents.pulse.models import PulseTaskInput

    assert set(PulseTaskInput.model_fields) == {
        "trade_case_id",
        "task_id",
        "market_pair_id",
        "trigger",
        "observations",
        "window_truncated",
        "latest",
        "policy_version",
        "evaluated_at",
    }
    rendered = (await read(now, evidence=(setup_envelope(now),))).model_dump_json()
    for absent in ("bars", "structure", "summary", "reason_codes", "history", "sentiment"):
        assert absent not in rendered


async def test_the_view_records_which_policy_judged_it(now):
    context = await read(now, evidence=(setup_envelope(now),))
    assert context.policy_version == PULSE_TRIGGER_V1.version


def test_the_reader_holds_no_transport_and_no_writer():
    from dataclasses import fields

    names = {item.name for item in fields(PulseContextReader)}
    assert names == {"cases", "markets", "policy", "clock", "include_fixtures"}
    for forbidden in ("session", "client", "http", "url", "credential", "key", "token"):
        assert not any(forbidden in name for name in names)


@pytest.mark.parametrize("forbidden", ["session", "client", "provider_client", "rpc", "submit"])
def test_no_capability_can_arrive_through_the_view(forbidden):
    from src.agents.pulse.models import PulseTaskInput

    assert forbidden not in PulseTaskInput.model_fields
