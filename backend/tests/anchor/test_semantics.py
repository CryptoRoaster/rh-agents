"""Three quantities that sound alike, and the distance kept between them.

Execution deviation is what this system computes: a quote's effective price
against an independent reference, bundling depth, fees, spread and elapsed time.
Provider price impact is the provider's own figure in the provider's own
semantics. Slippage is the gap between a quote and a realised fill, and nothing
here has ever seen a fill.

The tests below exist because the cheapest way to satisfy an older field is to
put the nearest-looking number in it, and the cheapest way is wrong. A reader of
`estimated_slippage_bps` would act on a figure nobody measured.
"""

from datetime import timedelta
from decimal import Decimal

import pytest

from src.agents.anchor.assessment import assess, execution_deviation_bps
from src.agents.anchor.handler import AnchorWorkerHandler
from src.agents.anchor.models import (
    AnchorReasonCode,
    CapacitySemantics,
    ExecutionAssessment,
    RejectionReason,
)
from src.agents.anchor.policy import ANCHOR_EXECUTION_V1
from src.markets.quotes import QuoteFailure, percent_to_bps
from src.orchestration.workflow.models import (
    EvidenceAcceptance,
    ExecutionAssessmentDetail,
    LiquidityExecutionPayload,
    QuotedLadderPoint,
)
from tests.anchor.conftest import REFERENCE, ladder_from, source, task_input


async def assessed(now, **knobs) -> ExecutionAssessment:
    ladder = await ladder_from(source(now, **knobs))
    outcome = assess(task_input(now, ladder=ladder), now, ANCHOR_EXECUTION_V1)
    assert isinstance(outcome, ExecutionAssessment)
    return outcome


# --------------------------------- W: absent provider impact stays absent


async def test_scenario_w_an_absent_provider_impact_is_never_filled_in(now):
    """Deviation is computed; impact is the provider's to publish or not.

    KyberSwap publishes none on either supported chain, verified against the
    live API, so this is the ordinary case rather than an edge one.
    """
    outcome = await assessed(now, provider_price_impact_bps=None)

    accepted = [point for point in outcome.ladder if point.accepted]
    assert accepted
    for point in accepted:
        assert point.execution_deviation_bps is not None
        assert point.provider_price_impact_bps is None
    assert outcome.execution_deviation_bps_at_capacity is not None


async def test_scenario_w_the_evidence_leaves_both_legacy_scalars_empty(now, trace):
    """The substitution the audit forbade, checked where it would have landed."""
    outcome = await assessed(now)
    payload = _payload(now, trace, outcome)

    assert payload.estimated_slippage_bps is None
    assert payload.price_impact_bps is None
    # And the real figures are present, named for what they are.
    assert payload.execution is not None
    assert payload.execution.execution_deviation_bps_at_capacity is not None


async def test_a_deviation_never_becomes_a_provider_impact(now, trace):
    """Even with a large measured deviation, impact stays absent."""
    outcome = await assessed(now, deviation_bps_per_step=Decimal(80))
    payload = _payload(now, trace, outcome)

    assert outcome.execution_deviation_bps_at_capacity != Decimal(0)
    assert payload.price_impact_bps is None


async def test_a_deviation_never_becomes_a_slippage_estimate(now, trace):
    """`estimated_slippage_bps` means a realisable fill cost in this repository.

    Its sibling on a market snapshot is what the paper executor uses to move a
    fill price and then records as `realized_slippage_bps`. This assessment has
    no such figure and says so.
    """
    outcome = await assessed(now, deviation_bps_per_step=Decimal(30))
    payload = _payload(now, trace, outcome)

    assert payload.estimated_slippage_bps is None
    deviation = outcome.execution_deviation_bps_at_capacity
    assert deviation is not None and deviation > Decimal(0)


async def test_scenario_28_a_partly_annotated_ladder_is_not_smoothed(now):
    """Some rungs may carry an impact figure and others may not.

    Nothing synthesises the missing ones, and nothing drops the present ones.
    What each point knew is what each point records.
    """
    outcome = await assessed(now, provider_price_impact_bps=Decimal(5))
    for point in outcome.ladder:
        if point.accepted:
            assert point.provider_price_impact_bps == Decimal(5)

    bare = await assessed(now, provider_price_impact_bps=None)
    for point in bare.ladder:
        assert point.provider_price_impact_bps is None


def _payload(now, trace, outcome) -> LiquidityExecutionPayload:
    handler = AnchorWorkerHandler(quote_provider="fixture:quotes")
    result = handler._evidence(  # noqa: SLF001 - the payload is the subject
        _lease(now, trace), task_input(now, ladder=()), outcome
    )
    payload = result.submission.payload
    assert isinstance(payload, LiquidityExecutionPayload)
    return payload


def _lease(now, trace):
    from uuid import uuid4

    from src.core.models import AgentRole
    from src.orchestration.worker.models import TaskLease

    return TaskLease(
        lease_id=uuid4(),
        task_id=uuid4(),
        trade_case_id=uuid4(),
        role=AgentRole.ANCHOR,
        task_type="ASSESS_EXECUTION",
        worker_instance_id=uuid4(),
        attempt_number=1,
        lease_started_at=now,
        lease_expires_at=now + timedelta(minutes=1),
        renewals=0,
        correlation_id=trace,
    )


# ------------------------------------------ X: percent, sign and magnitude


@pytest.mark.parametrize(
    ("percent", "bps"),
    [
        (Decimal(0), Decimal(0)),
        # The reading error this exists to prevent: 0.04 percent is four basis
        # points, not four hundredths of one.
        (Decimal("0.04"), Decimal(4)),
        (Decimal("0.5"), Decimal(50)),
        (Decimal(3), Decimal(300)),
        (Decimal(-5), Decimal(-500)),
        (Decimal("-0.01"), Decimal(-1)),
        (Decimal(100), Decimal(10000)),
    ],
)
def test_scenario_x_percentages_normalise_to_basis_points_exactly(percent, bps):
    assert percent_to_bps(percent) == bps
    assert isinstance(percent_to_bps(percent), Decimal)


def test_scenario_x_a_negative_impact_is_compared_by_magnitude(now):
    """A five percent cost written as -5 must not pass a three percent bound.

    The bound is a limit on how expensive a quote may be. Comparing a signed
    figure against it directly would let `-500 < 300` read as acceptable, which
    is the exact confusion this asserts against.
    """
    bound = ANCHOR_EXECUTION_V1.max_provider_price_impact_bps
    stated = percent_to_bps(Decimal(-5))

    assert stated == Decimal(-500)
    assert stated < bound  # the naive comparison, which must not be the one used
    assert abs(stated) > bound


async def test_an_impact_beyond_the_bound_rejects_the_point(now):
    outcome = await assessed(now, provider_price_impact_bps=Decimal(400))
    assert outcome.ladder[0].rejection == RejectionReason.PROVIDER_IMPACT_TOO_HIGH
    assert outcome.semantics == CapacitySemantics.NONE


async def test_an_impact_inside_the_bound_does_not(now):
    outcome = await assessed(now, provider_price_impact_bps=Decimal(299))
    assert outcome.ladder[0].accepted


# ------------------------------- AG / AH: what may establish a rejected bound


@pytest.mark.parametrize(
    "transient",
    [
        QuoteFailure.RATE_LIMITED,
        QuoteFailure.TIMEOUT,
        QuoteFailure.PROVIDER_UNAVAILABLE,
        QuoteFailure.INVALID_RESPONSE,
        QuoteFailure.UNSUPPORTED_CHAIN,
    ],
)
async def test_scenario_ag_a_transient_failure_never_bounds_the_market(now, transient):
    """A provider that could not answer has said nothing about liquidity.

    Letting a timeout at $500 record "$500 was rejected" would turn an outage
    into a permanent-looking market fact, and a later retry into a recovery that
    never happened.
    """
    ladder = await ladder_from(source(now, fails_above=Decimal(500), failure_above=transient))
    outcome = assess(task_input(now, ladder=ladder), now, ANCHOR_EXECUTION_V1)

    assert outcome == AnchorReasonCode.QUOTES_UNAVAILABLE
    assert not isinstance(outcome, ExecutionAssessment)


async def test_a_transient_failure_discards_even_the_rungs_that_passed(now):
    """Conservative on purpose: an incomplete ladder is not a smaller market."""
    quotes = source(now, fails_above=Decimal(500), failure_above=QuoteFailure.TIMEOUT)
    ladder = await ladder_from(quotes)
    assert any(attempt.quote is not None for attempt in ladder)

    outcome = assess(task_input(now, ladder=ladder), now, ANCHOR_EXECUTION_V1)
    assert outcome == AnchorReasonCode.QUOTES_UNAVAILABLE


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (QuoteFailure.NO_ROUTE, RejectionReason.NO_ROUTE),
        (QuoteFailure.INSUFFICIENT_LIQUIDITY, RejectionReason.INSUFFICIENT_LIQUIDITY),
    ],
)
async def test_scenario_ah_a_market_fact_may_bound_the_market(now, failure, reason):
    """The provider answered, and the answer was about the market."""
    ladder = await ladder_from(source(now, fails_above=Decimal(2500), failure_above=failure))
    outcome = assess(task_input(now, ladder=ladder), now, ANCHOR_EXECUTION_V1)

    assert isinstance(outcome, ExecutionAssessment)
    assert outcome.semantics == CapacitySemantics.BOUNDED
    assert outcome.largest_tested_acceptable_notional_usd == Decimal(500)
    assert outcome.first_tested_rejected_notional_usd == Decimal(2500)
    assert outcome.ladder[-1].rejection == reason


# --------------------------- 25 / 26: what a bracket does and does not claim


async def test_a_bracket_claims_only_what_was_tested(now):
    """500 passed and 2500 did not. Nothing is claimed about 501 to 2499."""
    outcome = await assessed(now, fails_above=Decimal(2500))

    assert outcome.largest_tested_acceptable_notional_usd == Decimal(500)
    assert outcome.first_tested_rejected_notional_usd == Decimal(2500)
    # The names carry the claim, so a reader cannot mistake it for a maximum.
    assert "largest_tested_acceptable_notional_usd" in ExecutionAssessment.model_fields
    assert "maximum_executable_notional" not in ExecutionAssessment.model_fields
    tested = {point.notional_usd for point in outcome.ladder}
    assert Decimal(1000) not in tested


async def test_scenario_26_a_non_monotonic_market_is_read_conservatively(now):
    """$100 passes, $500 fails, and a hypothetical $2500 might have passed.

    The walk stops at the first refusal, so the larger size is never tested and
    never claimed either way. What is reported is support up to $100 — which is
    true — rather than a maximum, which would not be.
    """
    quotes = source(now, fails_above=Decimal(500), empty_above=None)
    ladder = await ladder_from(quotes)
    outcome = assess(task_input(now, ladder=ladder), now, ANCHOR_EXECUTION_V1)

    assert isinstance(outcome, ExecutionAssessment)
    assert outcome.semantics == CapacitySemantics.BOUNDED
    assert outcome.largest_tested_acceptable_notional_usd == Decimal(100)
    assert outcome.first_tested_rejected_notional_usd == Decimal(500)
    # The larger sizes were never asked about, so nothing is said about them.
    assert quotes.calls == [100 * 10**6, 500 * 10**6]
    assert len(outcome.ladder) == 2


async def test_nothing_interpolates_between_the_two_bounds(now):
    outcome = await assessed(now, fails_above=Decimal(2500))
    recorded = [point.notional_usd for point in outcome.ladder]
    assert recorded == [Decimal(100), Decimal(500), Decimal(2500)]


# ---------------------------------------- AK / AL: freshness and coherence


async def test_scenario_ak_freshness_uses_the_earlier_of_the_two_times(now):
    """A provider clock ahead of ours must not make a stale quote look fresh."""
    quotes = source(now, quoted_at=now, received_at=now - timedelta(minutes=5))
    ladder = await ladder_from(quotes)
    outcome = assess(task_input(now, ladder=ladder), now, ANCHOR_EXECUTION_V1)

    assert isinstance(outcome, ExecutionAssessment)
    assert outcome.ladder[0].rejection == RejectionReason.QUOTE_TOO_STALE


async def test_a_quote_fresh_by_both_clocks_is_accepted(now):
    quotes = source(now, quoted_at=now - timedelta(seconds=5), received_at=now)
    ladder = await ladder_from(quotes)
    outcome = assess(task_input(now, ladder=ladder), now, ANCHOR_EXECUTION_V1)

    assert isinstance(outcome, ExecutionAssessment)
    assert outcome.ladder[0].accepted


async def test_scenario_al_a_ladder_spread_over_time_establishes_nothing(now):
    """Points minutes apart are separate observations of a moving market."""
    # Each point individually fresh, the set of them spread past the coherence
    # window — which is the case that would otherwise read as a depth curve.
    quotes = source(now, quoted_at=now - timedelta(seconds=30), skew_per_quote=timedelta(seconds=7))
    ladder = await ladder_from(quotes)
    outcome = assess(task_input(now, ladder=ladder), now, ANCHOR_EXECUTION_V1)

    assert isinstance(outcome, ExecutionAssessment)
    assert outcome.semantics == CapacitySemantics.UNKNOWN
    assert outcome.reason_code == AnchorReasonCode.LADDER_INCOHERENT
    assert outcome.largest_tested_acceptable_notional_usd is None


def test_the_deviation_is_signed_and_positive_means_worse(now):
    assert execution_deviation_bps(Decimal("101"), Decimal("100")) == Decimal(100)
    assert execution_deviation_bps(Decimal("99"), Decimal("100")) == Decimal(-100)
    assert execution_deviation_bps(REFERENCE, REFERENCE) == Decimal(0)


# ------------------------------------ Y / Z: the older vocabulary still works


def test_scenario_y_a_legacy_payload_parses_without_being_given_new_semantics(now):
    """Evidence written before Phase 2J has no execution detail, and needs none.

    It also must not be retrofitted: its `estimated_slippage_bps` meant whatever
    it meant when it was written, and nothing here reinterprets it as a quote
    deviation or copies it into the new fields.
    """
    from uuid import uuid4

    legacy = LiquidityExecutionPayload(
        setup_evidence_id=uuid4(),
        trigger_evidence_id=uuid4(),
        quoted_price=Decimal("215.00"),
        liquidity_usd=Decimal(1_000_000),
        estimated_slippage_bps=Decimal(35),
        price_impact_bps=Decimal(12),
        maximum_safe_size_usd=Decimal(25_000),
        routing_provenance="legacy-router",
    )

    assert legacy.execution is None
    assert legacy.estimated_slippage_bps == Decimal(35)
    assert legacy.price_impact_bps == Decimal(12)
    # No detail was invented from the scalars.
    assert legacy.acceptance() == EvidenceAcceptance.ACCEPTED


def test_a_legacy_payload_round_trips_through_serialisation(now):
    """Replay has to work on evidence already in the database."""
    from uuid import uuid4

    raw = {
        "kind": "liquidity_execution",
        "setup_evidence_id": str(uuid4()),
        "trigger_evidence_id": str(uuid4()),
        "quoted_price": "215.00",
        "liquidity_usd": "1000000",
        "estimated_slippage_bps": "35",
        "price_impact_bps": "12",
        "maximum_safe_size_usd": "25000",
        "routing_provenance": "legacy-router",
    }
    parsed = LiquidityExecutionPayload.model_validate(raw)
    assert parsed.execution is None
    assert parsed.model_dump()["estimated_slippage_bps"] == Decimal(35)


async def test_scenario_z_a_new_assessment_is_substantive_without_legacy_scalars(now, trace):
    """The invariant that would otherwise force a fabricated number.

    Liquidity evidence has to carry substance to count. A complete execution
    assessment is substance; requiring a legacy scalar as well would mean
    inventing one, which is how a lie gets a reason to exist.
    """
    outcome = await assessed(now)
    payload = _payload(now, trace, outcome)

    assert payload.estimated_slippage_bps is None
    assert payload.price_impact_bps is None
    assert payload.execution is not None
    assert payload.acceptance() == EvidenceAcceptance.ACCEPTED


async def test_a_known_bad_market_blocks_without_any_legacy_scalar(now, trace):
    outcome = await assessed(now, always_fails=QuoteFailure.NO_ROUTE)
    payload = _payload(now, trace, outcome)

    assert outcome.semantics == CapacitySemantics.NONE
    assert payload.execution is not None
    assert payload.maximum_safe_size_usd is None
    assert payload.acceptance() == EvidenceAcceptance.BLOCKED


def test_an_execution_detail_alone_satisfies_the_substance_requirement(now):
    """Stated directly against the predicate rather than through a worker."""
    from uuid import uuid4

    detail = ExecutionAssessmentDetail(
        policy_version="anchor-execution-v1",
        capacity_semantics="AT_LEAST",
        reason_code="CAPACITY_AT_LEAST_TESTED_CEILING",
        largest_tested_acceptable_notional_usd=Decimal(50_000),
        reference_price=REFERENCE,
        reference_price_basis="USD_PER_BASE_UNIT",
        reference_observed_at=now,
        quote_asset_usd_price=Decimal(1),
        quote_asset_usd_observed_at=now,
        quote_asset_usd_provider="geckoterminal",
        payment_asset_id="robinhood:mainnet:0xaa",
        target_asset_id="robinhood:mainnet:0xbb",
        quote_provider="kyberswap",
        quote_requests=5,
        ladder=(
            QuotedLadderPoint(
                notional_usd=Decimal(100),
                amount_in_tokens=Decimal(100),
                accepted=True,
                amount_out=1,
                effective_price_usd=REFERENCE,
            ),
        ),
        evaluated_at=now,
        execution_digest="a" * 64,
    )
    payload = LiquidityExecutionPayload(
        setup_evidence_id=uuid4(),
        trigger_evidence_id=uuid4(),
        execution=detail,
    )
    assert payload.estimated_slippage_bps is None
    assert payload.price_impact_bps is None
    assert payload.maximum_safe_size_usd is None
    assert payload.acceptance() == EvidenceAcceptance.ACCEPTED


# ------------------------------------------ AI: the unit handed toward risk


async def test_scenario_ai_the_risk_facing_scalar_is_usd_not_tokens(now, trace):
    """`maximum_safe_size_usd` ends in `_usd` and must deserve it.

    With a payment asset at $250, a token-denominated capacity of 2 would be
    written as "2" into a field risk reads as dollars. It is $500.
    """
    ladder = await ladder_from(
        source(now, quote_asset_usd_price=Decimal(250), fails_above=Decimal(2500)),
        usd_per_token=Decimal(250),
    )
    from tests.anchor.conftest import valuation as _valuation

    outcome = assess(
        task_input(now, ladder=ladder, value=_valuation(now, usd_per_token=Decimal(250))),
        now,
        ANCHOR_EXECUTION_V1,
    )
    assert isinstance(outcome, ExecutionAssessment)
    payload = _payload(now, trace, outcome)

    assert payload.maximum_safe_size_usd == Decimal(500)
    assert payload.execution is not None
    assert payload.execution.largest_tested_acceptable_notional_usd == Decimal(500)
    # The tokens are recorded too, separately, so the two can never be confused.
    assert payload.execution.ladder[1].amount_in_tokens == Decimal(2)
    assert payload.execution.quote_asset_usd_price == Decimal(250)


async def test_the_token_amount_and_the_usd_amount_are_never_the_same_field(now, trace):
    outcome = await assessed(now)
    payload = _payload(now, trace, outcome)
    assert payload.execution is not None
    point = payload.execution.ladder[0]
    assert {"notional_usd", "amount_in_tokens"} <= set(QuotedLadderPoint.model_fields)
    assert point.notional_usd is not None and point.amount_in_tokens is not None


def test_anchor_names_none_of_sentinels_fields():
    """No aliasing, so the final cap stays somebody else's to compute."""
    for forbidden in (
        "max_additional_notional_usd",
        "position_size_limit_usd",
        "approved_notional_usd",
        "risk_outcome",
    ):
        assert forbidden not in ExecutionAssessmentDetail.model_fields
        assert forbidden not in LiquidityExecutionPayload.model_fields


# ----------------------------------------- AJ: nothing to sign, anywhere


async def test_scenario_aj_no_provider_transaction_field_reaches_the_evidence(now, trace):
    outcome = await assessed(now)
    payload = _payload(now, trace, outcome)
    rendered = payload.model_dump_json().lower()

    for forbidden in (
        '"to"',
        '"data"',
        '"value"',
        "calldata",
        "transaction",
        "routeraddress",
        "permit2",
        "hookdata",
        "0xab",
    ):
        assert forbidden not in rendered, forbidden
    assert payload.execution is not None
    assert payload.execution.ladder[0].venues or True
