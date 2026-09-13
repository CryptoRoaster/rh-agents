"""What "$500" means, and everything that goes wrong when nobody asks.

The ladder is written in dollars because SENTINEL sizes in dollars. The provider
is asked in base units of whatever the market is paid in. Those are the same
number only when the payment asset happens to trade at a dollar, and the whole
of this file exists because nothing is allowed to assume it does.

The failure this prevents is quiet. A ladder of 100/500/2500 sent as token
amounts against a payment asset worth $600 would test six hundred times what it
claimed, every quote would come back plausible, and the capacity written into
evidence would be wrong by that factor under a field name ending in `_usd`.
Nothing would raise, and risk would size against it.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from src.agents.anchor.assessment import assess, valuation_skew_bps
from src.agents.anchor.context import AnchorContextReader, tokens_for_usd
from src.agents.anchor.models import (
    AnchorReasonCode,
    CapacitySemantics,
    ExecutionAssessment,
    RejectionReason,
)
from src.agents.anchor.policy import ANCHOR_EXECUTION_V1
from src.agents.anchor.ports import AnchorContextUnavailable
from src.core.clock import FixedClock
from src.markets.models import Availability
from tests.anchor.conftest import (
    CHAIN,
    NETWORK,
    QUOTE_TOKEN,
    REFERENCE,
    StubCases,
    StubMarkets,
    StubTradeCase,
    evidence_envelope,
    market_identity,
    payment_snapshot,
    setup_payload,
    source,
    task_input,
    trigger_payload,
    valuation,
)
from tests.anchor.test_context import snapshot_for

QUOTE_ASSET = f"{CHAIN}:{NETWORK}:{QUOTE_TOKEN}"


# --------------------------------------------------- AA: the conversion itself


@pytest.mark.parametrize(
    ("usd", "price", "decimals", "tokens", "base_units"),
    [
        # The case the audit named: a payment asset worth $250.
        (Decimal(500), Decimal(250), 18, Decimal(2), 2 * 10**18),
        # A dollar asset, where the two numbers coincide and nothing is proven.
        (Decimal(500), Decimal(1), 6, Decimal(500), 500 * 10**6),
        # An expensive one, where a token-denominated ladder would have tested
        # six hundred times what it claimed.
        (Decimal(600), Decimal(600), 18, Decimal(1), 10**18),
        # Not representable exactly: truncated down, never up.
        (Decimal(500), Decimal("0.97"), 6, Decimal("515.463917"), 515_463_917),
    ],
)
def test_a_usd_rung_becomes_the_right_number_of_tokens(usd, price, decimals, tokens, base_units):
    assert tokens_for_usd(usd, price, decimals) == (tokens, base_units)


def test_the_conversion_never_asks_for_more_than_intended():
    """Truncation is downward, so a rung never tests a larger size than it says."""
    tokens, _ = tokens_for_usd(Decimal(500), Decimal("0.97"), 6)
    assert tokens * Decimal("0.97") <= Decimal(500)


def test_a_payment_asset_without_a_price_cannot_be_converted():
    with pytest.raises(ValueError):
        tokens_for_usd(Decimal(500), Decimal(0), 18)


# ------------------------------------------- AA: the ladder through the reader


async def read(now, *, usd_per_token=Decimal(1), quotes=None, payment="default"):
    identity = market_identity()
    trade_case = StubTradeCase(identity)
    setup = evidence_envelope(
        now,
        __import__("src.orchestration.workflow.models", fromlist=["x"]).EvidenceType.TRADE_SETUP,
        __import__("src.core.models", fromlist=["x"]).AgentRole.VECTOR,
        setup_payload(now),
        trade_case_id=trade_case.id,
    )
    trigger = evidence_envelope(
        now,
        __import__("src.orchestration.workflow.models", fromlist=["x"]).EvidenceType.TRIGGER,
        __import__("src.core.models", fromlist=["x"]).AgentRole.PULSE,
        trigger_payload(setup.evidence_id),
        trade_case_id=trade_case.id,
    )
    pair = snapshot_for(now)
    markets = StubMarkets(
        pair,
        payment=(payment_snapshot(pair, usd_per_token) if payment == "default" else payment),
    )
    reader = AnchorContextReader(
        cases=StubCases(trade_case, (setup, trigger)),
        markets=markets,
        quotes=quotes if quotes is not None else source(now, quote_asset_usd_price=usd_per_token),
        clock=FixedClock(now),
        include_fixtures=True,
    )
    return await reader.execution_context(trade_case.id, trade_case.id)


async def test_scenario_aa_a_non_dollar_payment_asset_converts_before_quoting(now):
    """A $250 asset: the $500 rung sends two tokens, not five hundred."""
    context = await read(now, usd_per_token=Decimal(250))

    assert context.quote_asset_valuation is not None
    assert context.quote_asset_valuation.usd_per_token == Decimal(250)
    # The ladder still speaks dollars.
    assert [attempt.notional_usd for attempt in context.ladder][:3] == [
        Decimal(100),
        Decimal(500),
        Decimal(2500),
    ]
    # And the provider was asked for tokens.
    assert [attempt.amount_in_tokens for attempt in context.ladder][:3] == [
        Decimal("0.4"),
        Decimal(2),
        Decimal(10),
    ]
    # Six decimals for this payment asset, taken from the recorded pair.
    assert context.ladder[1].amount_in == 2 * 10**6


async def test_a_dollar_ladder_sent_as_tokens_would_have_been_six_hundred_times_too_big(now):
    """The defect this file exists for, stated as a number."""
    context = await read(now, usd_per_token=Decimal(600))
    rung = context.ladder[0]
    assert rung.notional_usd < Decimal(100)  # the $100 rung, truncated to the unit

    # What the old code sent for this rung: the ladder number, as tokens.
    naive_tokens = Decimal(100)
    naive_usd = naive_tokens * Decimal(600)
    assert naive_usd == Decimal(60_000)
    assert abs(naive_usd / rung.notional_usd - Decimal(600)) < Decimal("0.01")
    # What it sends now.
    assert rung.amount_in_tokens < Decimal(1)


async def test_scenario_ab_a_depegged_stablecoin_is_not_worth_a_dollar(now):
    """$0.97 observed. No peg, no symbol matching, no rounding to one."""
    context = await read(now, usd_per_token=Decimal("0.97"))

    rung = next(a for a in context.ladder if a.notional_usd <= Decimal(100))
    # 100 / 0.97 = 103.092783505..., truncated to the token's six decimals.
    assert rung.amount_in_tokens == Decimal("103.092783")
    assert rung.amount_in == 103_092_783
    # The rung records the size it actually tested, not the one it aimed at.
    assert rung.notional_usd == Decimal("103.092783") * Decimal("0.97")
    assert rung.notional_usd < Decimal(100)


async def test_a_stablecoin_is_never_assumed_to_be_one(now):
    """Nothing reads a symbol, so nothing can be fooled by one."""
    depegged = await read(now, usd_per_token=Decimal("0.50"))
    rung = depegged.ladder[0]
    assert rung.amount_in_tokens == Decimal(200)
    assert rung.amount_in_tokens != Decimal(100)


# ------------------------------------------------ AC: no valuation, no quoting


async def test_scenario_ac_an_unvalued_payment_asset_stops_before_any_request(now):
    """Fails closed, and spends nothing finding out.

    A ladder nobody can denominate is a ladder nobody may use, so it is never
    asked for. The provider request budget is not spent building a number that
    would have to be discarded.
    """
    quotes = source(now)
    with pytest.raises(AnchorContextUnavailable) as error:
        await read(now, quotes=quotes, payment=None)

    assert error.value.reason_code == "QUOTE_ASSET_USD_VALUE_UNAVAILABLE"
    assert quotes.calls == []


async def test_an_unpriced_payment_asset_is_not_a_thin_market(now):
    """The distinction that keeps a missing price from reading as missing depth."""
    pair = snapshot_for(now)
    unpriced = pair.model_copy(
        update={
            "id": pair.id,
            "price": pair.price.model_copy(
                update={"value_usd": None, "status": Availability.UNAVAILABLE}
            ),
        }
    )
    quotes = source(now)
    with pytest.raises(AnchorContextUnavailable) as error:
        await read(now, quotes=quotes, payment=unpriced)

    assert error.value.reason_code == "QUOTE_ASSET_USD_VALUE_UNAVAILABLE"
    assert quotes.calls == []


async def test_a_stale_payment_price_is_refused_like_a_missing_one(now):
    """An old valuation would silently misprice every rung on the ladder."""
    pair = snapshot_for(now)
    stale = payment_snapshot(pair)
    stale = stale.model_copy(
        update={
            "price": stale.price.model_copy(
                update={"observed_at": now - timedelta(hours=2)},
            )
        }
    )
    quotes = source(now)
    with pytest.raises(AnchorContextUnavailable):
        await read(now, quotes=quotes, payment=stale)
    assert quotes.calls == []


def test_an_assessment_without_a_valuation_refuses_rather_than_assuming_one(now):
    outcome = assess(task_input(now, ladder=(), value=None), now, ANCHOR_EXECUTION_V1)
    assert outcome == AnchorReasonCode.QUOTE_ASSET_USD_VALUE_UNAVAILABLE


def test_a_stale_valuation_refuses_at_assessment_too(now):
    """Checked on both sides, because the reader and the evaluator can disagree."""
    outcome = assess(
        task_input(now, ladder=(), value=valuation(now, seconds_ago=3600)),
        now,
        ANCHOR_EXECUTION_V1,
    )
    assert outcome == AnchorReasonCode.QUOTE_ASSET_USD_VALUE_UNAVAILABLE


# ----------------------------------------- the deviation is measured in dollars


async def test_the_deviation_is_a_usd_comparison_not_a_token_one(now):
    """Reference is USD per unit bought; a quote is payment-tokens per unit.

    Comparing them unconverted would measure the payment asset's own price. With
    a $250 payment asset and no depth cost, an unconverted comparison would
    report a deviation of about minus a hundred percent.
    """
    quotes = source(now, quote_asset_usd_price=Decimal(250), deviation_bps_per_step=Decimal(0))
    context = await read(now, usd_per_token=Decimal(250), quotes=quotes)
    outcome = assess(context, now, ANCHOR_EXECUTION_V1)

    assert isinstance(outcome, ExecutionAssessment)
    assert outcome.quote_asset_usd_price == Decimal(250)
    accepted = [point for point in outcome.ladder if point.accepted]
    assert accepted
    for point in accepted:
        assert point.effective_price_usd is not None
        # Priced in dollars per share, near the reference, not near 1/250 of it.
        assert abs(point.effective_price_usd - REFERENCE) < Decimal("0.01")
        assert abs(point.execution_deviation_bps) < Decimal(1)


async def test_capacity_is_reported_in_dollars_for_a_non_dollar_asset(now):
    quotes = source(now, quote_asset_usd_price=Decimal(250), fails_above=Decimal(2500))
    context = await read(now, usd_per_token=Decimal(250), quotes=quotes)
    outcome = assess(context, now, ANCHOR_EXECUTION_V1)

    assert isinstance(outcome, ExecutionAssessment)
    assert outcome.semantics == CapacitySemantics.BOUNDED
    # Dollars supported, not tokens: two tokens of a $250 asset is $500.
    assert outcome.largest_tested_acceptable_notional_usd == Decimal(500)
    assert outcome.first_tested_rejected_notional_usd == Decimal(2500)


# ------------------------------------- AD: our valuation against the provider's


@pytest.mark.parametrize(
    ("ours", "theirs", "expected"),
    [
        (Decimal(100), Decimal(100), Decimal(0)),
        (Decimal(100), Decimal("100.04"), Decimal(4)),
        (Decimal(100), Decimal("99.96"), Decimal(4)),
        # A decimals error is wrong by factors, not percents.
        (Decimal(100), Decimal(1_000_000), Decimal(99_990_000)),
    ],
)
def test_valuation_skew_is_an_absolute_basis_point_figure(ours, theirs, expected):
    assert valuation_skew_bps(ours, theirs) == expected


async def test_scenario_ad_a_material_valuation_disagreement_rejects_the_point(now):
    """Neither side is trusted over the other; the quote is simply unreadable.

    A provider valuing the order at a hundred times ours means one of us has the
    decimals wrong, has the wrong token, or is pricing from a stale feed. None of
    those produce economics worth comparing, so the point is rejected rather than
    silently accepted or silently preferred.
    """
    quotes = source(now, quote_usd_skew=Decimal(100))
    context = await read(now, quotes=quotes)
    outcome = assess(context, now, ANCHOR_EXECUTION_V1)

    assert isinstance(outcome, ExecutionAssessment)
    assert outcome.ladder[0].rejection == RejectionReason.USD_VALUATION_DISAGREEMENT
    assert outcome.semantics == CapacitySemantics.NONE
    assert outcome.largest_tested_acceptable_notional_usd is None


async def test_a_small_valuation_difference_is_tolerated(now):
    """Two honest feeds moments apart must not collide."""
    quotes = source(now, quote_usd_skew=Decimal("1.004"))
    context = await read(now, quotes=quotes)
    outcome = assess(context, now, ANCHOR_EXECUTION_V1)

    assert isinstance(outcome, ExecutionAssessment)
    assert outcome.ladder[0].accepted
    assert outcome.ladder[0].provider_amount_in_usd is not None


async def test_an_absent_provider_valuation_is_not_a_disagreement(now):
    """KyberSwap publishes one; a provider that does not must not be penalised."""
    context = await read(now, quotes=source(now, quote_usd_skew=None))
    outcome = assess(context, now, ANCHOR_EXECUTION_V1)

    assert isinstance(outcome, ExecutionAssessment)
    assert outcome.ladder[0].accepted
    assert outcome.ladder[0].provider_amount_in_usd is None


async def test_the_provider_valuation_never_decides_how_much_to_send(now):
    """It arrives with the answer, so it cannot have shaped the question.

    The amounts requested are identical whether the provider annotates them or
    not, which is what makes the cross-check a check rather than a circle.
    """
    annotated = source(now, quote_usd_skew=Decimal(1))
    bare = source(now, quote_usd_skew=None)
    await read(now, quotes=annotated)
    await read(now, quotes=bare)
    assert annotated.calls == bare.calls
