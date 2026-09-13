"""Assembling the view: which setup, which reference, and how many quotes.

The context reader is where provider traffic is generated, so the questions here
are about bounds and bindings: does it run after a trigger and only after one,
does it ask for exactly the assets the market names, and can any input make it
ask a provider for more than a handful of quotes.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.agents.anchor.context import AnchorContextReader, market_context, reference_market
from src.agents.anchor.policy import ANCHOR_EXECUTION_V1
from src.agents.anchor.ports import AnchorContextUnavailable
from src.core.clock import FixedClock
from src.core.models import AgentRole
from src.markets.fake import fixture_snapshot
from src.markets.models import Availability
from src.markets.quotes import (
    QuoteFailure,
    UnconfiguredQuoteSource,
    to_base_units,
    to_human,
)
from src.orchestration.workflow.models import EvidenceStatus, EvidenceType
from tests.anchor.conftest import (
    BASE_TOKEN,
    PAIR_ID,
    QUOTE_DECIMALS,
    QUOTE_TOKEN,
    REFERENCE,
    StubCases,
    StubMarkets,
    StubTradeCase,
    evidence_envelope,
    market_identity,
    source,
    trigger_payload,
    triggered_pair,
)


def snapshot_for(now, *, price=REFERENCE, seconds_ago=10, pair_id=PAIR_ID, decimals=True):
    base = fixture_snapshot(now - timedelta(seconds=seconds_ago), uuid4())
    pair = base.pair.model_copy(
        update={
            "pair_id": pair_id,
            "base": base.pair.base.model_copy(update={"decimals": 18 if decimals else None}),
            "quote": base.pair.quote.model_copy(
                update={"decimals": QUOTE_DECIMALS if decimals else None}
            ),
        }
    )
    priced = base.price.model_copy(
        update={
            "status": Availability.AVAILABLE if price is not None else Availability.UNKNOWN,
            "value_usd": price,
        }
    )
    return base.model_copy(update={"pair": pair, "price": priced})


async def read(now, *, evidence=None, snapshot="default", quotes=None, market=None):
    if evidence is None:
        evidence = triggered_pair(now)
    reader = AnchorContextReader(
        cases=StubCases(StubTradeCase(market or market_identity()), evidence),
        markets=StubMarkets(snapshot_for(now) if snapshot == "default" else snapshot),
        quotes=quotes or source(now),
        clock=FixedClock(now),
        include_fixtures=True,
    )
    return await reader.execution_context(uuid4(), uuid4())


# ---------------------------------------------- ANCHOR runs after a trigger


async def test_a_triggered_setup_supplies_the_binding(now):
    setup, trigger = triggered_pair(now)
    context = await read(now, evidence=(setup, trigger))
    assert context.setup_evidence_id == setup.evidence_id
    assert context.trigger_evidence_id == trigger.evidence_id
    assert context.setup_fingerprint == setup.payload.setup.setup_fingerprint
    assert context.market.pair_id == PAIR_ID


async def test_no_trigger_means_no_assessment(now):
    """Scenario ordering: VECTOR, then PULSE, then ANCHOR. Never collapsed."""
    setup, _ = triggered_pair(now)
    with pytest.raises(AnchorContextUnavailable) as error:
        await read(now, evidence=(setup,))
    assert error.value.reason_code == "NO_TRIGGERED_SETUP"


async def test_a_trigger_for_another_setup_is_refused(now):
    """The trigger must belong to the setup that is currently authoritative."""
    setup, _ = triggered_pair(now)
    stray = evidence_envelope(now, EvidenceType.TRIGGER, AgentRole.PULSE, trigger_payload(uuid4()))
    with pytest.raises(AnchorContextUnavailable) as error:
        await read(now, evidence=(setup, stray))
    assert error.value.reason_code == "TRIGGER_NOT_FOR_CURRENT_SETUP"


async def test_an_unusable_setup_supplies_no_binding(now):
    setup, trigger = triggered_pair(now)
    unknown = setup.model_copy(update={"status": EvidenceStatus.UNKNOWN})
    with pytest.raises(AnchorContextUnavailable):
        await read(now, evidence=(unknown, trigger))


async def test_a_superseded_setup_is_never_assessed(now):
    """Selection goes through the workflow's own answer, not a heuristic."""
    setup, trigger = triggered_pair(now)
    replacement = evidence_envelope(
        now,
        EvidenceType.TRADE_SETUP,
        AgentRole.VECTOR,
        setup.payload,
        supersedes_id=setup.evidence_id,
    )
    # The trigger names the old setup, which is no longer current.
    with pytest.raises(AnchorContextUnavailable) as error:
        await read(now, evidence=(setup, replacement, trigger))
    assert error.value.reason_code == "TRIGGER_NOT_FOR_CURRENT_SETUP"


# ------------------------------------------------- assets and decimals


async def test_the_quote_asks_for_the_markets_own_assets(now):
    """Not a wrapped native token by convention. The assets the market names."""
    quotes = source(now)
    context = await read(now, quotes=quotes)
    assert context.market.quote_token == QUOTE_TOKEN
    assert context.market.base_token == BASE_TOKEN
    assert context.market.quote_decimals == QUOTE_DECIMALS
    assert context.market.base_decimals == 18
    # A hundred dollars of a six-decimal asset is 1e8 base units, not 1e20.
    assert quotes.calls[0] == 100 * 10**QUOTE_DECIMALS


async def test_unknown_decimals_fail_closed(now):
    """Assuming eighteen is how a hundred-dollar order becomes a trillion-dollar one."""
    with pytest.raises(AnchorContextUnavailable) as error:
        await read(now, snapshot=snapshot_for(now, decimals=False))
    assert error.value.reason_code == "TOKEN_DECIMALS_UNKNOWN"


def test_base_unit_conversion_is_exact_in_both_directions():
    assert to_base_units(Decimal("100"), 6) == 100_000_000
    assert to_human(100_000_000, 6) == Decimal("100")
    assert to_base_units(Decimal("0.000001"), 6) == 1


def test_an_amount_finer_than_the_token_is_refused():
    """Rounding it down would place an order the caller did not ask for."""
    with pytest.raises(ValueError):
        to_base_units(Decimal("0.0000001"), 6)


# --------------------------------------------------- the reference price


async def test_the_reference_is_the_current_market_price(now):
    context = await read(now)
    assert context.reference is not None
    assert context.reference.price == REFERENCE
    assert context.reference.price_basis == "USD_PER_BASE_UNIT"


async def test_an_unpriced_market_supplies_no_reference(now):
    context = await read(now, snapshot=snapshot_for(now, price=None))
    assert context.reference is None


def test_a_non_positive_reference_is_never_supplied(now):
    snapshot = snapshot_for(now)
    zeroed = snapshot.price.model_copy(
        update={"status": Availability.AVAILABLE, "value_usd": Decimal(0)}
    )
    assert reference_market(snapshot.model_copy(update={"price": zeroed}), now) is None


async def test_the_reference_is_not_the_setup_entry_or_the_trigger_level(now):
    """PULSE proved a threshold was crossed. ANCHOR prices the market now."""
    setup, trigger = triggered_pair(now)
    context = await read(now, evidence=(setup, trigger))
    assert context.reference.price == REFERENCE
    assert context.reference.price != setup.payload.entry_price
    assert context.reference.price != trigger.payload.observed_price


# ---------------------------------------------------- the bounded ladder


async def test_the_ladder_walks_the_policys_sizes(now):
    quotes = source(now, deviation_bps_per_step=Decimal(1))
    context = await read(now, quotes=quotes)
    assert [attempt.notional for attempt in context.ladder] == list(
        ANCHOR_EXECUTION_V1.ladder_notional
    )
    assert context.quote_requests == len(ANCHOR_EXECUTION_V1.ladder_notional)


async def test_the_ladder_stops_at_the_first_refusal(now):
    """No point asking for more once the market has said no."""
    quotes = source(now, fails_above=Decimal(2500), failure_above=QuoteFailure.NO_ROUTE)
    context = await read(now, quotes=quotes)
    assert len(context.ladder) == 3
    assert context.ladder[-1].failure == QuoteFailure.NO_ROUTE
    assert len(quotes.calls) == 3


async def test_no_input_can_make_the_ladder_unbounded(now):
    """Scenario R. A provider that always says yes still gets a handful of calls."""
    quotes = source(now, deviation_bps_per_step=Decimal(0))
    await read(now, quotes=quotes)
    assert len(quotes.calls) <= ANCHOR_EXECUTION_V1.max_quote_requests
    assert len(quotes.calls) == len(ANCHOR_EXECUTION_V1.ladder_notional)


def test_a_policy_whose_budget_cannot_cover_its_ladder_refuses_to_exist():
    """The bound is a configuration fact, checked once, not a branch per call.

    A budget smaller than the ladder would describe an assessment that can never
    finish, so the policy refuses rather than leaving a guard inside the loop
    that could never fire.
    """
    from dataclasses import replace

    with pytest.raises(ValueError):
        replace(
            ANCHOR_EXECUTION_V1,
            ladder_notional=tuple(Decimal(100) * (index + 1) for index in range(10)),
            max_quote_requests=3,
        )


def test_the_ladder_is_small_enough_to_audit():
    """Twelve points is the hard cap; five is what ships."""
    from dataclasses import replace

    assert len(ANCHOR_EXECUTION_V1.ladder_notional) == 5
    with pytest.raises(ValueError):
        replace(
            ANCHOR_EXECUTION_V1,
            ladder_notional=tuple(Decimal(100) * (index + 1) for index in range(13)),
            max_quote_requests=32,
        )


async def test_an_unconfigured_provider_asks_for_nothing_and_says_so(now):
    """No quote source wired is stated, never treated as an empty market."""
    context = await read(now, quotes=UnconfiguredQuoteSource())
    assert len(context.ladder) == 1
    assert context.ladder[0].failure == QuoteFailure.NOT_CONFIGURED


def test_a_reader_without_a_configured_source_refuses_by_default():
    reader = AnchorContextReader(cases=object(), markets=object())  # type: ignore[arg-type]
    assert isinstance(reader.quotes, UnconfiguredQuoteSource)


# --------------------------------------------------------- the surface


def test_the_reader_holds_no_transport_and_no_writer():
    from dataclasses import fields

    names = {item.name for item in fields(AnchorContextReader)}
    assert names == {"cases", "markets", "quotes", "policy", "clock", "include_fixtures"}
    for forbidden in ("session", "client", "http", "url", "credential", "key", "token"):
        assert not any(forbidden in name for name in names)


async def test_the_assembled_input_carries_no_client_session_or_url(now):
    rendered = (await read(now)).model_dump_json()
    for forbidden in ("http://", "https://", "postgresql", "api_key", "Authorization"):
        assert forbidden not in rendered


async def test_a_market_reading_for_another_pool_is_refused(now):
    wrong = snapshot_for(now, pair_id=f"{market_identity().chain}:mainnet:contract_address:0xff")
    with pytest.raises(AnchorContextUnavailable) as error:
        await read(now, snapshot=wrong)
    assert error.value.reason_code == "MARKET_IDENTITY_MISMATCH"


async def test_no_market_data_at_all_stops_the_assessment(now):
    with pytest.raises(AnchorContextUnavailable) as error:
        await read(now, snapshot=None)
    assert error.value.reason_code == "MARKET_OBSERVATION_MISSING"


def test_market_context_requires_both_decimals(now):
    snapshot = snapshot_for(now, decimals=False)
    with pytest.raises(AnchorContextUnavailable):
        market_context(snapshot, StubTradeCase(market_identity()))
