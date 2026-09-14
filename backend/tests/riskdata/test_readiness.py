"""The completeness check, against the real workflow service and a real database.

Nothing is stubbed except the single market read and the stop source. The case,
its evidence, its supersession chain and its status all go through
`TradeCaseService`.
"""

from datetime import timedelta
from uuid import uuid4

import pytest

from src.core.models import TradingMode
from src.orchestration.riskdata.context import RiskDataReader, RiskDataUnavailable
from src.orchestration.riskdata.models import (
    RiskDataGapCode,
    RiskDataOutcome,
    RiskFactKind,
    RiskFactOrigin,
)
from src.orchestration.riskdata.policy import RISK_DATA_V1
from src.orchestration.workflow.models import EvidenceType
from tests.riskdata.conftest import (
    BASE_ASSET,
    PAIR_ID,
    RecordedMarkets,
    build_reader,
    configured_costs,
    holder_block,
    onchain_payload,
    prepare_case,
    record_onchain,
    recorded_snapshot,
)


def gap(reading, kind):
    return next((item for item in reading.gaps if item.kind is kind), None)


def fact(reading, kind):
    return next((item for item in reading.facts if item.kind is kind), None)


# ------------------------------------------------------------------- complete


async def test_a_fully_supplied_case_reports_complete_data(worker_db, now, trace):
    """Every checked input present, current and attributed."""
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    trade_case = await prepare_case(reader.cases, now, trace)

    reading = await reader.readiness(trade_case.id)

    assert reading.complete
    assert reading.outcome is RiskDataOutcome.RISK_DATA_COMPLETE
    assert reading.gaps == ()
    assert {item.kind for item in reading.facts} == set(RiskFactKind)
    assert reading.base_asset_id == BASE_ASSET
    assert reader.markets.requested == [(PAIR_ID, False)]


async def test_every_fact_names_its_asset_unit_and_source(worker_db, now, trace):
    """Provenance is the point: a number without one cannot be checked."""
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    trade_case = await prepare_case(reader.cases, now, trace)

    reading = await reader.readiness(trade_case.id)

    price = fact(reading, RiskFactKind.REFERENCE_PRICE)
    assert price.meaning == "USD_PER_BASE_UNIT"
    assert price.asset_id == BASE_ASSET
    assert price.origin is RiskFactOrigin.RECORDED_MARKET_OBSERVATION
    assert price.valid_until == price.observed_at + RISK_DATA_V1.max_price_age

    concentration = fact(reading, RiskFactKind.HOLDER_CONCENTRATION)
    assert concentration.meaning == "TOP_TEN_FRACTION_OF_TOTAL_SUPPLY"
    assert concentration.origin is RiskFactOrigin.ATLAS_ONCHAIN_EVIDENCE
    assert concentration.asset_id == BASE_ASSET

    routing = fact(reading, RiskFactKind.ROUTING_AVAILABILITY)
    assert routing.origin is RiskFactOrigin.ANCHOR_EXECUTION_EVIDENCE


async def test_completeness_is_only_about_the_data_it_checked(worker_db, now, trace):
    """No authorisation, no execution, no state change of any kind.

    The case does not move, no risk binding appears, and the reading itself
    carries nothing that resembles permission.
    """
    from sqlalchemy import func, select

    from src.data.tables import TradeCaseRiskBindingRow

    _, sessions = worker_db
    reader = build_reader(sessions, now)
    trade_case = await prepare_case(reader.cases, now, trace)
    before = await reader.cases.get_trade_case(trade_case.id)
    envelopes = len(await reader.cases.evidence(trade_case.id))

    reading = await reader.readiness(trade_case.id)
    assert reading.complete

    after = await reader.cases.get_trade_case(trade_case.id)
    assert (after.status, after.revision) == (before.status, before.revision)
    assert len(await reader.cases.evidence(trade_case.id)) == envelopes
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(TradeCaseRiskBindingRow)) == 0
    for forbidden in ("approved", "authorization", "permitted", "size", "quantity"):
        assert not any(forbidden in name for name in type(reading).model_fields)
    assert [item.value for item in RiskDataOutcome] == ["RISK_DATA_COMPLETE"]


# ------------------------------------------------------------- market sources


async def test_an_unrecorded_market_leaves_three_gaps(worker_db, now, trace):
    _, sessions = worker_db
    reader = build_reader(sessions, now, feed=RecordedMarkets(None))
    trade_case = await prepare_case(reader.cases, now, trace)

    reading = await reader.readiness(trade_case.id)

    assert not reading.complete
    for kind in (
        RiskFactKind.REFERENCE_PRICE,
        RiskFactKind.TOKEN_METADATA,
        RiskFactKind.LIQUIDITY_DEPTH,
    ):
        assert gap(reading, kind).code is RiskDataGapCode.NOT_RECORDED


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(seconds=89), None),
        (RISK_DATA_V1.max_price_age, RiskDataGapCode.STALE),
        (timedelta(seconds=200), RiskDataGapCode.STALE),
        (timedelta(seconds=-1), RiskDataGapCode.OBSERVED_IN_THE_FUTURE),
    ],
)
async def test_price_freshness_at_and_around_the_boundary(worker_db, now, trace, age, expected):
    """Half-open, like every other validity here."""
    _, sessions = worker_db
    feed = RecordedMarkets(recorded_snapshot(now, age=age))
    reader = build_reader(sessions, now, feed=feed)
    trade_case = await prepare_case(reader.cases, now, trace)

    reading = await reader.readiness(trade_case.id)

    found = gap(reading, RiskFactKind.REFERENCE_PRICE)
    assert (None if found is None else found.code) is expected


async def test_a_price_for_another_asset_is_not_this_token_s_price(worker_db, now, trace):
    """Wrong by the exchange rate, and entirely plausible-looking."""
    _, sessions = worker_db
    other = f"robinhood:mainnet:{'0x' + 'f9' * 20}"
    feed = RecordedMarkets(recorded_snapshot(now, base_asset_id=other))
    reader = build_reader(sessions, now, feed=feed)
    trade_case = await prepare_case(reader.cases, now, trace)

    reading = await reader.readiness(trade_case.id)

    assert gap(reading, RiskFactKind.REFERENCE_PRICE).code is RiskDataGapCode.WRONG_ASSET
    assert gap(reading, RiskFactKind.TOKEN_METADATA).code is RiskDataGapCode.WRONG_ASSET


async def test_unknown_decimals_are_never_assumed_to_be_eighteen(worker_db, now, trace):
    _, sessions = worker_db
    feed = RecordedMarkets(recorded_snapshot(now, decimals=None))
    reader = build_reader(sessions, now, feed=feed)
    trade_case = await prepare_case(reader.cases, now, trace)

    reading = await reader.readiness(trade_case.id)

    assert gap(reading, RiskFactKind.TOKEN_METADATA).code is RiskDataGapCode.NOT_ESTABLISHED


async def test_an_unavailable_liquidity_reading_is_a_gap(worker_db, now, trace):
    _, sessions = worker_db
    feed = RecordedMarkets(recorded_snapshot(now, liquidity=None))
    reader = build_reader(sessions, now, feed=feed)
    trade_case = await prepare_case(reader.cases, now, trace)

    reading = await reader.readiness(trade_case.id)

    assert gap(reading, RiskFactKind.LIQUIDITY_DEPTH).code is RiskDataGapCode.NOT_ESTABLISHED


# ------------------------------------------------------------- holder semantics


async def test_a_pass_verdict_never_substitutes_for_a_measurement(worker_db, now, trace):
    """The substitution this phase exists to prevent.

    `holder_integrity == "PASS"` says the holder domain met its data-quality
    prerequisites. It is not a concentration and not a count, and an evidence
    row carrying the verdict without the numbers yields gaps rather than facts.
    """
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    payload = onchain_payload(now, holders=None)
    trade_case = await prepare_case(reader.cases, now, trace, onchain=payload)

    reading = await reader.readiness(trade_case.id)

    assert fact(reading, RiskFactKind.HOLDER_INTEGRITY) is not None
    assert gap(reading, RiskFactKind.HOLDER_COUNT).code is RiskDataGapCode.VERDICT_WITHOUT_METRIC
    assert (
        gap(reading, RiskFactKind.HOLDER_CONCENTRATION).code
        is RiskDataGapCode.VERDICT_WITHOUT_METRIC
    )


async def test_an_unproven_coverage_supports_no_concentration(worker_db, now, trace):
    """A source that could not say what it saw supports no metric at all."""
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    payload = onchain_payload(now, holders=holder_block(now, completeness="UNKNOWN"))
    trade_case = await prepare_case(reader.cases, now, trace, onchain=payload)

    reading = await reader.readiness(trade_case.id)

    assert (
        gap(reading, RiskFactKind.HOLDER_CONCENTRATION).code
        is RiskDataGapCode.HOLDER_COVERAGE_UNPROVEN
    )
    # The count is unaffected: it never depended on our paging.
    assert fact(reading, RiskFactKind.HOLDER_COUNT) is not None


async def test_a_provider_filtered_holder_list_understates_concentration(worker_db, now, trace):
    """An understated figure passing a limit is what a limit exists to prevent.

    Rows the provider removed are missing from the numerator while the
    denominator stays full on-chain supply, so the metric is a lower bound and
    may not feed a threshold — however clean the verdict beside it looks.
    """
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    excluded = ("0x" + "0" * 39 + "1",)
    payload = onchain_payload(now, holders=holder_block(now, excluded=excluded))
    trade_case = await prepare_case(reader.cases, now, trace, onchain=payload)

    reading = await reader.readiness(trade_case.id)

    assert (
        gap(reading, RiskFactKind.HOLDER_CONCENTRATION).code
        is RiskDataGapCode.HOLDER_METRIC_UNDERSTATED
    )


async def test_a_top_n_prefix_still_supports_the_top_ten_share(worker_db, now, trace):
    """A balance-ordered prefix of at least ten proves the top-ten share exactly."""
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    payload = onchain_payload(now, holders=holder_block(now, completeness="TOP_N_ONLY"))
    trade_case = await prepare_case(reader.cases, now, trace, onchain=payload)

    reading = await reader.readiness(trade_case.id)

    assert fact(reading, RiskFactKind.HOLDER_CONCENTRATION) is not None


async def test_a_missing_holder_count_is_its_own_gap(worker_db, now, trace):
    """Count and concentration come from different things and fail separately."""
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    payload = onchain_payload(now, holders=holder_block(now, holder_count=None))
    trade_case = await prepare_case(reader.cases, now, trace, onchain=payload)

    reading = await reader.readiness(trade_case.id)

    assert gap(reading, RiskFactKind.HOLDER_COUNT).code is RiskDataGapCode.NOT_ESTABLISHED
    assert fact(reading, RiskFactKind.HOLDER_CONCENTRATION) is not None


async def test_holder_facts_observed_in_the_future_are_refused(worker_db, now, trace):
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    payload = onchain_payload(now, holders=holder_block(now, age=timedelta(seconds=-5)))
    trade_case = await prepare_case(reader.cases, now, trace, onchain=payload)

    reading = await reader.readiness(trade_case.id)

    for kind in (RiskFactKind.HOLDER_COUNT, RiskFactKind.HOLDER_CONCENTRATION):
        assert gap(reading, kind).code is RiskDataGapCode.OBSERVED_IN_THE_FUTURE


async def test_an_unknown_domain_establishes_nothing_at_all(worker_db, now, trace):
    """Not even a negative fact. An unmeasured domain is a gap."""
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    payload = onchain_payload(now, contract="UNKNOWN", holders_verdict="UNKNOWN", holders=None)
    trade_case = await prepare_case(reader.cases, now, trace, onchain=payload)

    reading = await reader.readiness(trade_case.id)

    assert gap(reading, RiskFactKind.TOKEN_TRADABILITY).code is RiskDataGapCode.NOT_ESTABLISHED
    assert gap(reading, RiskFactKind.HOLDER_INTEGRITY).code is RiskDataGapCode.NOT_ESTABLISHED
    assert gap(reading, RiskFactKind.HOLDER_COUNT).code is RiskDataGapCode.NOT_ESTABLISHED


# ----------------------------------------------------------- staleness, chains


async def test_stale_onchain_evidence_withdraws_every_fact_it_supplied(worker_db, now, trace):
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    trade_case = await prepare_case(reader.cases, now, trace)

    later = build_reader(sessions, now + timedelta(hours=2))
    reading = await later.readiness(trade_case.id)

    for kind in (
        RiskFactKind.TOKEN_TRADABILITY,
        RiskFactKind.HOLDER_INTEGRITY,
        RiskFactKind.HOLDER_COUNT,
        RiskFactKind.HOLDER_CONCENTRATION,
    ):
        assert gap(reading, kind).code is RiskDataGapCode.STALE


async def test_a_superseded_envelope_is_never_the_one_read(worker_db, now, trace):
    """Supersession through the real service, not a copied identifier."""
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    trade_case = await prepare_case(reader.cases, now, trace)
    first = next(
        item
        for item in await reader.cases.evidence(trade_case.id)
        if item.evidence_type is EvidenceType.ONCHAIN
    )

    await record_onchain(
        reader.cases,
        trade_case,
        now,
        onchain_payload(now, holders=holder_block(now, holder_count=99)),
        key=f"riskdata-atlas-2-{trade_case.id}",
        supersedes_id=first.evidence_id,
    )
    reading = await reader.readiness(trade_case.id)

    assert reading.complete
    assert fact(reading, RiskFactKind.HOLDER_COUNT) is not None


async def test_a_missing_anchor_finding_leaves_routing_unestablished(worker_db, now, trace):
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    trade_case = await prepare_case(reader.cases, now, trace, anchor=False)

    reading = await reader.readiness(trade_case.id)

    assert gap(reading, RiskFactKind.ROUTING_AVAILABILITY).code is RiskDataGapCode.NOT_ESTABLISHED


async def test_an_unknown_case_cannot_be_assessed(worker_db, now, trace):
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    with pytest.raises(RiskDataUnavailable):
        await reader.readiness(uuid4())


# ------------------------------------------------------------------ cost basis


async def test_complete_data_without_a_cost_basis_is_incomplete(worker_db, now, trace):
    """Everything measured, and still no answer about what trading costs."""
    _, sessions = worker_db
    reader = build_reader(sessions, now, costs=configured_costs(fee=None, slippage=None))
    trade_case = await prepare_case(reader.cases, now, trace)

    reading = await reader.readiness(trade_case.id)

    assert not reading.complete
    for kind in (RiskFactKind.EXECUTION_FEE_BASIS, RiskFactKind.EXECUTION_SLIPPAGE_BASIS):
        assert gap(reading, kind).code is RiskDataGapCode.NOT_CONFIGURED
    # Everything else is still reported as present.
    assert fact(reading, RiskFactKind.REFERENCE_PRICE) is not None
    assert fact(reading, RiskFactKind.HOLDER_CONCENTRATION) is not None


async def test_a_partly_configured_cost_basis_is_no_cost_basis(worker_db, now, trace):
    _, sessions = worker_db
    reader = build_reader(sessions, now, costs=configured_costs(slippage=None))
    trade_case = await prepare_case(reader.cases, now, trace)

    reading = await reader.readiness(trade_case.id)

    assert not reading.complete
    assert gap(reading, RiskFactKind.EXECUTION_FEE_BASIS).code is RiskDataGapCode.NOT_CONFIGURED


async def test_cost_facts_are_marked_as_assumptions_not_observations(worker_db, now, trace):
    """Configuration wearing no disguise: no source market, no observation time."""
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    trade_case = await prepare_case(reader.cases, now, trace)

    reading = await reader.readiness(trade_case.id)

    for kind in (RiskFactKind.EXECUTION_FEE_BASIS, RiskFactKind.EXECUTION_SLIPPAGE_BASIS):
        item = fact(reading, kind)
        assert item.origin is RiskFactOrigin.OPERATOR_CONFIGURED_ASSUMPTION
        assert item.observed_at is None
        assert item.valid_until is None
        assert item.asset_id is None


async def test_an_observe_deployment_has_no_paper_cost_basis(worker_db, now, trace):
    _, sessions = worker_db
    reader = build_reader(sessions, now, costs=configured_costs(mode=TradingMode.OBSERVE))
    trade_case = await prepare_case(reader.cases, now, trace)

    reading = await reader.readiness(trade_case.id)

    assert gap(reading, RiskFactKind.EXECUTION_FEE_BASIS).code is RiskDataGapCode.NOT_CONFIGURED


# -------------------------------------------------------- blockers versus gaps


async def test_a_known_violation_is_a_blocker_and_still_a_fact(worker_db, now, trace):
    """Measured and dangerous is not the same as unmeasured.

    The tradability value is established — it is the bad one — so it travels as
    a fact a risk evaluation can read, and as a blocker an operator can see.
    """
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    payload = onchain_payload(now, contract="FAIL", blockers=("CONTRACT_CODE_ABSENT",))
    trade_case = await prepare_case(reader.cases, now, trace, onchain=payload)

    reading = await reader.readiness(trade_case.id)

    assert fact(reading, RiskFactKind.TOKEN_TRADABILITY) is not None
    assert gap(reading, RiskFactKind.TOKEN_TRADABILITY) is None
    codes = {item.code for item in reading.blockers}
    assert "CONTRACT_CODE_ABSENT" in codes
    assert "ATLAS_EVIDENCE_BLOCKED" in codes


async def test_blockers_and_gaps_are_reported_together(worker_db, now, trace):
    """A case can be both incompletely measured and known to be dangerous."""
    _, sessions = worker_db
    reader = build_reader(sessions, now, feed=RecordedMarkets(None))
    payload = onchain_payload(now, contract="FAIL", blockers=("CONTRACT_CODE_ABSENT",))
    trade_case = await prepare_case(reader.cases, now, trace, onchain=payload)

    reading = await reader.readiness(trade_case.id)

    assert not reading.complete
    assert reading.gaps
    assert reading.blockers
    assert not any(
        item.code is RiskDataGapCode.NOT_ESTABLISHED
        for item in reading.gaps
        if item.kind is RiskFactKind.TOKEN_TRADABILITY
    )


async def test_an_unreadable_stop_is_a_blocker_rather_than_silence(worker_db, now, trace):
    """Unknown is not permission, here as everywhere else."""
    _, sessions = worker_db
    reader = build_reader(sessions, now, pause=None)
    trade_case = await prepare_case(reader.cases, now, trace)

    reading = await reader.readiness(trade_case.id)

    assert "SYSTEM_STOP_UNREADABLE" in {item.code for item in reading.blockers}
    # Data completeness is a separate question and is still answered.
    assert reading.complete


async def test_a_paused_system_is_reported_beside_complete_data(worker_db, now, trace):
    from tests.riskdata.conftest import RunningSystem

    _, sessions = worker_db
    reader = build_reader(sessions, now, pause=RunningSystem(paused=True))
    trade_case = await prepare_case(reader.cases, now, trace)

    reading = await reader.readiness(trade_case.id)

    assert reading.complete
    assert "SYSTEM_PAUSED" in {item.code for item in reading.blockers}


async def test_a_cancelled_case_is_reported_as_terminal(worker_db, now, trace):
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    trade_case = await prepare_case(reader.cases, now, trace)
    await reader.cases.cancel_trade_case(trade_case.id)

    reading = await reader.readiness(trade_case.id)

    assert "TRADE_CASE_TERMINAL" in {item.code for item in reading.blockers}


async def test_incomplete_data_never_becomes_a_risk_verdict(worker_db, now, trace):
    """A gap stops the check. It does not manufacture a rejection.

    A rejection here would be terminal and would permanently bar the market from
    opening another case — an architectural hole spending a real decision.
    """
    _, sessions = worker_db
    reader = build_reader(sessions, now, feed=RecordedMarkets(None))
    trade_case = await prepare_case(reader.cases, now, trace)
    before = await reader.cases.get_trade_case(trade_case.id)

    reading = await reader.readiness(trade_case.id)

    assert not reading.complete
    assert reading.outcome is None
    after = await reader.cases.get_trade_case(trade_case.id)
    assert after.status == before.status != "RISK_REJECTED"


# ---------------------------------------------------------------- reader shape


def test_the_reader_takes_no_clock_and_no_writable_port():
    import inspect

    from src.orchestration.riskdata.context import RiskDataCaseSource, RiskDataMarketInput

    assert set(inspect.signature(RiskDataReader.readiness).parameters) == {
        "self",
        "trade_case_id",
    }
    assert {name for name in vars(RiskDataCaseSource) if not name.startswith("_")} == {
        "get_trade_case",
        "evidence",
    }
    assert {name for name in vars(RiskDataMarketInput) if not name.startswith("_")} == {"latest"}


async def test_the_reading_ages_out_with_its_shortest_lived_fact(worker_db, now, trace):
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    trade_case = await prepare_case(reader.cases, now, trace)

    reading = await reader.readiness(trade_case.id)

    horizons = [item.valid_until for item in reading.facts if item.valid_until is not None]
    assert reading.valid_until == min(horizons)
    assert reading.is_current_at(now)
    assert not reading.is_current_at(reading.valid_until)
