"""The deterministic capacity assessment: what the market was shown to support.

This is the load-bearing file of the phase. The question is not whether a ladder
can be walked but whether the answer it produces can be misread — and the answer
that matters most is the one where every tested size passed, because reporting
the top of a bounded search as a limit would turn a floor into a ceiling in the
mind of whoever sizes against it.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from src.agents.anchor.assessment import assess, execution_deviation_bps
from src.agents.anchor.models import (
    AnchorReasonCode,
    CapacitySemantics,
    RejectionReason,
)
from src.agents.anchor.policy import ANCHOR_EXECUTION_V1
from src.markets.quotes import QuoteFailure
from tests.anchor.conftest import (
    REFERENCE,
    anchor_market,
    ladder_from,
    reference,
    source,
    task_input,
)


async def run(now, quotes=None, **kwargs):
    ladder = await ladder_from(quotes or source(now), kwargs.pop("market", None))
    return assess(task_input(now, ladder=ladder, **kwargs), now, ANCHOR_EXECUTION_V1)


# ------------------------------------------------- A: a bracketed capacity


async def test_scenario_a_a_failing_size_brackets_the_capacity(now):
    """The ordinary outcome: some sizes work, a larger one does not."""
    result = await run(now)
    assert result.semantics == CapacitySemantics.BOUNDED
    assert result.reason_code == AnchorReasonCode.CAPACITY_BRACKETED
    assert result.largest_tested_acceptable_notional_usd == Decimal(2500)
    assert result.first_tested_rejected_notional_usd == Decimal(10000)
    assert result.is_executable is True


async def test_the_ladder_records_why_each_size_passed_or_failed(now):
    """ "Why did ANCHOR say 2500?" is answered by the rejection as much as the pass."""
    result = await run(now)
    by_size = {point.notional_usd: point for point in result.ladder}
    assert by_size[Decimal(500)].accepted is True
    # Base units are integers, so the quoted output truncates and the deviation
    # recovered from it is not exactly the figure the fixture priced at. That is
    # a real property of quoting in indivisible units, not a rounding bug.
    assert abs(by_size[Decimal(500)].execution_deviation_bps - Decimal(10)) < Decimal("0.001")
    rejected = by_size[Decimal(10000)]
    assert rejected.accepted is False
    assert rejected.rejection == RejectionReason.EXECUTION_DEVIATION_TOO_HIGH
    assert abs(rejected.execution_deviation_bps - Decimal(200)) < Decimal("0.001")
    # Every point carries what it was quoted, not just the accepted one.
    assert rejected.amount_out is not None and rejected.route_hops is not None


async def test_the_ladder_stops_at_the_first_failure(now):
    """Capacity is monotone in intent, so continuing would spend requests to learn nothing."""
    result = await run(now)
    assert [point.notional_usd for point in result.ladder] == [
        Decimal(100),
        Decimal(500),
        Decimal(2500),
        Decimal(10000),
    ]
    assert Decimal(50000) not in {point.notional_usd for point in result.ladder}


async def test_nothing_is_interpolated_between_the_bracket(now):
    """The answer is two tested facts, not a guess at what lies between them."""
    result = await run(now)
    tested = {point.notional_usd for point in result.ladder}
    assert result.largest_tested_acceptable_notional_usd in tested
    assert result.first_tested_rejected_notional_usd in tested


# ------------------------------------- B: everything passed is not a maximum


async def test_scenario_b_a_ladder_that_never_failed_reports_a_floor(now):
    """The most important distinction in the phase.

    A bounded search that passed every size it tried has learned that the market
    supports *at least* that much. Reporting the top of the ladder as a maximum
    would hand a downstream reader a ceiling that was never measured.
    """
    shallow = source(now, deviation_bps_per_step=Decimal(1))
    result = await run(now, shallow)
    assert result.semantics == CapacitySemantics.AT_LEAST
    assert result.reason_code == AnchorReasonCode.CAPACITY_AT_LEAST_TESTED_CEILING
    assert result.largest_tested_acceptable_notional_usd == max(ANCHOR_EXECUTION_V1.ladder_notional)
    # Nothing was rejected, so nothing brackets it.
    assert result.first_tested_rejected_notional_usd is None
    assert all(point.accepted for point in result.ladder)


def test_an_at_least_capacity_can_never_carry_a_rejected_size(now):
    """Enforced by the type, not by the caller remembering."""
    from src.agents.anchor.models import ExecutionAssessment, QuotedPoint

    point = QuotedPoint(
        notional_usd=Decimal(100),
        amount_in_tokens=Decimal(100),
        accepted=True,
        amount_out=1,
        effective_price_usd=REFERENCE,
    )
    with pytest.raises(ValueError):
        ExecutionAssessment(
            policy_version="anchor-execution-v1",
            semantics=CapacitySemantics.AT_LEAST,
            reason_code=AnchorReasonCode.CAPACITY_AT_LEAST_TESTED_CEILING,
            largest_tested_acceptable_notional_usd=Decimal(100),
            first_tested_rejected_notional_usd=Decimal(500),
            reference_price=REFERENCE,
            ladder=(point,),
            quote_requests=1,
            evaluated_at=now,
        )


def test_a_bracketed_capacity_must_name_the_size_that_failed(now):
    from src.agents.anchor.models import ExecutionAssessment, QuotedPoint

    point = QuotedPoint(
        notional_usd=Decimal(100),
        amount_in_tokens=Decimal(100),
        accepted=True,
        amount_out=1,
        effective_price_usd=REFERENCE,
    )
    with pytest.raises(ValueError):
        ExecutionAssessment(
            policy_version="anchor-execution-v1",
            semantics=CapacitySemantics.BOUNDED,
            reason_code=AnchorReasonCode.CAPACITY_BRACKETED,
            largest_tested_acceptable_notional_usd=Decimal(100),
            reference_price=REFERENCE,
            ladder=(point,),
            quote_requests=1,
            evaluated_at=now,
        )


def test_a_rejected_size_must_sit_above_the_supported_one(now):
    from src.agents.anchor.models import ExecutionAssessment, QuotedPoint

    point = QuotedPoint(
        notional_usd=Decimal(500),
        amount_in_tokens=Decimal(500),
        accepted=True,
        amount_out=1,
        effective_price_usd=REFERENCE,
    )
    with pytest.raises(ValueError):
        ExecutionAssessment(
            policy_version="anchor-execution-v1",
            semantics=CapacitySemantics.BOUNDED,
            reason_code=AnchorReasonCode.CAPACITY_BRACKETED,
            largest_tested_acceptable_notional_usd=Decimal(500),
            first_tested_rejected_notional_usd=Decimal(100),
            reference_price=REFERENCE,
            ladder=(point,),
            quote_requests=1,
            evaluated_at=now,
        )


# --------------------------------------------- C: nothing is supported


async def test_scenario_c_a_market_that_fails_the_smallest_size_supports_nothing(now):
    """A fact about the market, and reported without a figure.

    A capacity of zero would read as a measurement of an empty market; the
    absence of a figure says the market was observed and will not do this.
    """
    # Steep enough that even the smallest ladder step exceeds the bound.
    steep = source(now, deviation_bps_per_step=Decimal(5000))
    result = await run(now, steep)
    assert result.semantics == CapacitySemantics.NONE
    assert result.reason_code == AnchorReasonCode.NO_EXECUTABLE_CAPACITY
    assert result.largest_tested_acceptable_notional_usd is None
    assert result.is_executable is False
    assert len(result.ladder) == 1


def test_no_capacity_may_be_reported_with_a_figure(now):
    from src.agents.anchor.models import ExecutionAssessment, QuotedPoint

    point = QuotedPoint(
        notional_usd=Decimal(100),
        amount_in_tokens=Decimal(100),
        accepted=False,
        rejection=RejectionReason.NO_ROUTE,
    )
    for semantics in (CapacitySemantics.NONE, CapacitySemantics.UNKNOWN):
        with pytest.raises(ValueError):
            ExecutionAssessment(
                policy_version="anchor-execution-v1",
                semantics=semantics,
                reason_code=AnchorReasonCode.NO_EXECUTABLE_CAPACITY,
                largest_tested_acceptable_notional_usd=Decimal(100),
                reference_price=REFERENCE,
                ladder=(point,),
                quote_requests=1,
                evaluated_at=now,
            )


# ------------------------------------------------ D: no route is a fact


async def test_scenario_d_no_route_is_evidence_rather_than_an_outage(now):
    """The provider answered. What it said is that this cannot be traded."""
    result = await run(now, source(now, always_fails=QuoteFailure.NO_ROUTE))
    assert result.semantics == CapacitySemantics.NONE
    assert result.reason_code == AnchorReasonCode.NO_ROUTE
    assert result.ladder[0].rejection == RejectionReason.NO_ROUTE


async def test_a_market_that_runs_out_of_depth_brackets_rather_than_collapses(now):
    """Route exists for small sizes and not for large ones: still a bracket."""
    thin = source(now, fails_above=Decimal(2500), failure_above=QuoteFailure.INSUFFICIENT_LIQUIDITY)
    result = await run(now, thin)
    assert result.semantics == CapacitySemantics.BOUNDED
    assert result.largest_tested_acceptable_notional_usd == Decimal(500)
    assert result.first_tested_rejected_notional_usd == Decimal(2500)
    assert result.ladder[-1].rejection == RejectionReason.INSUFFICIENT_LIQUIDITY


async def test_a_quote_that_buys_nothing_is_rejected(now):
    result = await run(now, source(now, empty_above=Decimal(500)))
    assert result.largest_tested_acceptable_notional_usd == Decimal(100)
    assert result.ladder[-1].rejection == RejectionReason.NO_OUTPUT


# ------------------------------------------- E, F: absences of evidence


@pytest.mark.parametrize(
    "failure",
    [
        QuoteFailure.TIMEOUT,
        QuoteFailure.RATE_LIMITED,
        QuoteFailure.PROVIDER_UNAVAILABLE,
        QuoteFailure.INVALID_RESPONSE,
        QuoteFailure.NOT_CONFIGURED,
        QuoteFailure.UNSUPPORTED_CHAIN,
        QuoteFailure.BUDGET_EXCEEDED,
    ],
)
async def test_scenarios_e_and_f_a_provider_that_says_nothing_proves_nothing(now, failure):
    """Scenario E and F. An outage is never a measurement of an empty market.

    This is the distinction that would be most costly to collapse: a rate limit
    read as zero liquidity stops a trade for a reason that does not exist, and a
    later retry then looks like the market recovering.
    """
    result = await run(now, source(now, always_fails=failure))
    assert result == AnchorReasonCode.QUOTES_UNAVAILABLE


async def test_no_ladder_at_all_is_an_absence_rather_than_an_empty_market(now):
    assert assess(task_input(now, ladder=()), now) == AnchorReasonCode.QUOTES_UNAVAILABLE


# ------------------------------------------ G, H: freshness on both sides


async def test_scenario_g_a_stale_quote_cannot_establish_capacity(now):
    """An offer is perishable. Its price says what the market was."""
    stale = source(now, quoted_at=now - timedelta(minutes=5))
    result = await run(now, stale)
    assert result.semantics == CapacitySemantics.NONE
    assert result.ladder[0].rejection == RejectionReason.QUOTE_TOO_STALE


async def test_scenario_h_a_stale_reference_makes_deviation_meaningless(now):
    """A fresh quote against an old reference measures elapsed time, not cost."""
    result = await run(now, ref=reference(now, seconds_ago=600))
    assert result == AnchorReasonCode.REFERENCE_TOO_STALE


async def test_no_reference_at_all_stops_the_assessment(now):
    assert await run(now, ref=None) == AnchorReasonCode.REFERENCE_UNAVAILABLE


async def test_a_quote_from_the_future_is_refused(now):
    ahead = source(now, quoted_at=now + timedelta(minutes=5))
    result = await run(now, ahead)
    assert result.ladder[0].rejection == RejectionReason.QUOTE_IN_FUTURE


# --------------------------------------- I, J, K: identity before economics


async def test_scenario_i_a_quote_for_another_asset_is_not_a_cheap_quote(now):
    """Buying the wrong token is the most expensive mistake to miss."""
    wrong = source(now, override_token_out="0x" + "ff" * 20)
    result = await run(now, wrong)
    assert result.semantics == CapacitySemantics.UNKNOWN
    assert result.reason_code == AnchorReasonCode.LADDER_INCOHERENT
    assert result.ladder[0].rejection == RejectionReason.ASSET_MISMATCH


async def test_scenario_j_a_quote_from_another_chain_is_refused(now):
    result = await run(now, source(now, override_chain="bsc"))
    assert result.semantics == CapacitySemantics.UNKNOWN
    assert result.ladder[0].rejection == RejectionReason.CHAIN_MISMATCH


async def test_scenario_k_mismatched_decimals_are_an_identity_failure(now):
    """Decimals are part of what an amount means, so a mismatch is not arithmetic."""
    result = await run(now, market=anchor_market(quote_decimals=18))
    assert result.semantics == CapacitySemantics.UNKNOWN
    assert result.ladder[0].rejection == RejectionReason.ASSET_MISMATCH


async def test_identity_is_checked_before_the_price(now):
    """So a wrong-asset quote never reads as an attractive one."""
    generous = source(now, override_token_out="0x" + "ff" * 20, deviation_bps_per_step=Decimal(0))
    result = await run(now, generous)
    assert result.semantics == CapacitySemantics.UNKNOWN


# ------------------------------------------------ L: the route changes


async def test_scenario_l_a_route_that_grows_with_size_is_recorded_not_merged(now):
    """Real aggregators split larger orders. The ladder records each shape."""
    result = await run(now)
    hops = [point.route_hops for point in result.ladder]
    assert hops == sorted(hops)
    assert hops[0] < hops[-1]
    assert result.ladder[0].venues != result.ladder[-1].venues


async def test_a_route_beyond_the_complexity_bound_is_rejected(now):
    """A route nobody can follow is a route nobody can check."""
    tangled = source(now, hops_per_step=40, max_hops=60, deviation_bps_per_step=Decimal(0))
    result = await run(now, tangled)
    rejections = {point.rejection for point in result.ladder if point.rejection}
    assert RejectionReason.ROUTE_TOO_COMPLEX in rejections


# ------------------------------------------------- Q: an incoherent ladder


async def test_scenario_q_quotes_taken_too_far_apart_are_not_one_curve(now):
    """Several readings of a moving market are not a depth curve.

    Reading them as one would mistake the market moving for the market having
    depth, which is the error that would most flatter a thin market.
    """
    # Each successive quote is taken later, as a real ladder's would be, and the
    # span between first and last exceeds what one market state allows.
    drifting = source(
        now, quoted_at=now - timedelta(seconds=40), skew_per_quote=timedelta(seconds=10)
    )
    result = await run(now, drifting)
    assert result.semantics == CapacitySemantics.UNKNOWN
    assert result.reason_code == AnchorReasonCode.LADDER_INCOHERENT


async def test_a_ladder_within_the_skew_bound_is_coherent(now):
    tight = source(now, quoted_at=now - timedelta(seconds=8), skew_per_quote=timedelta(seconds=1))
    result = await run(now, tight)
    assert result.semantics == CapacitySemantics.BOUNDED


# ------------------------------------------- deviation, impact, slippage


def test_deviation_is_signed_and_positive_means_worse_for_a_buyer():
    assert execution_deviation_bps(Decimal(202), Decimal(200)) == Decimal(100)
    assert execution_deviation_bps(Decimal(198), Decimal(200)) == Decimal(-100)
    assert execution_deviation_bps(Decimal(200), Decimal(200)) == Decimal(0)


def test_a_reference_of_zero_cannot_be_compared_against():
    with pytest.raises(ValueError):
        execution_deviation_bps(Decimal(200), Decimal(0))


async def test_provider_impact_is_held_to_its_own_bound(now):
    """Never merged with the deviation: the two measure different things."""
    impactful = source(
        now, deviation_bps_per_step=Decimal(1), provider_price_impact_bps=Decimal(900)
    )
    result = await run(now, impactful)
    assert result.semantics == CapacitySemantics.NONE
    assert result.ladder[0].rejection == RejectionReason.PROVIDER_IMPACT_TOO_HIGH
    # The deviation was fine; it was the provider's own figure that failed.
    assert result.ladder[0].execution_deviation_bps < Decimal(10)


async def test_a_missing_provider_impact_is_simply_absent(now):
    """No figure is honest. A zero would be a claim the provider never made."""
    result = await run(now)
    assert all(point.provider_price_impact_bps is None for point in result.ladder)


def test_nothing_here_claims_to_know_realised_slippage():
    """ANCHOR measures an offer, not the outcome of a trade that has not happened."""
    import inspect

    from src.agents.anchor import assessment, models

    for module in (assessment, models):
        names = {name for name in dir(module) if not name.startswith("_")}
        assert not any("slippage" in name.lower() for name in names)
    source_text = inspect.getsource(models.QuotedPoint)
    assert "slippage" not in source_text.lower()


# ------------------------------------------------------ determinism


async def test_the_same_ladder_always_yields_the_same_assessment(now):
    ladder = await ladder_from(source(now))
    context = task_input(now, ladder=ladder)
    answers = {
        (
            assess(context, now).semantics,
            assess(context, now).largest_tested_acceptable_notional_usd,
        )
        for _ in range(20)
    }
    assert len(answers) == 1


def test_the_policy_is_versioned_and_coherent():
    assert ANCHOR_EXECUTION_V1.version == "anchor-execution-v1"
    assert list(ANCHOR_EXECUTION_V1.ladder_notional) == sorted(ANCHOR_EXECUTION_V1.ladder_notional)
    assert ANCHOR_EXECUTION_V1.max_quote_requests >= len(ANCHOR_EXECUTION_V1.ladder_notional)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_quote_age", timedelta(0)),
        ("max_reference_age", timedelta(0)),
        ("max_ladder_skew", timedelta(0)),
        ("max_ladder_skew", timedelta(minutes=10)),
        ("max_execution_deviation_bps", Decimal(0)),
        ("max_execution_deviation_bps", Decimal(20000)),
        ("max_provider_price_impact_bps", Decimal(0)),
        ("max_route_hops", 0),
        ("ladder_notional", ()),
        ("ladder_notional", (Decimal(500), Decimal(100))),
        ("ladder_notional", (Decimal(100), Decimal(100))),
        ("ladder_notional", (Decimal(-1),)),
        ("max_quote_requests", 1),
        ("max_quote_requests", 99),
    ],
)
def test_an_incoherent_policy_refuses_to_exist(field, value):
    from dataclasses import replace

    with pytest.raises(ValueError):
        replace(ANCHOR_EXECUTION_V1, **{field: value})


def test_the_policy_contains_no_trading_view():
    """Execution integrity only. No expected return, no signal, no probability."""
    from dataclasses import fields

    names = {item.name for item in fields(ANCHOR_EXECUTION_V1)}
    for forbidden in (
        "expected_return",
        "sentiment",
        "momentum",
        "probability",
        "confidence",
        "exposure",
        "cash",
        "daily_loss",
        "position",
    ):
        assert not any(forbidden in name for name in names)
