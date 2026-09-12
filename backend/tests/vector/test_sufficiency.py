"""Whether there is enough market structure to ask for a setup at all.

The defect this phase closed is reproduced first, deliberately, so the suite
records what the old behaviour was rather than merely asserting the new one: one
observed price used to be enough to produce an entry, an invalidation and two
targets. It no longer is, and everything below establishes where the line now
falls and that the model never gets to argue with it.
"""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.agents.vector.context import VectorContextReader, vector_input_digest
from src.agents.vector.handler import CONTEXT_FAILURES, VectorWorkerHandler
from src.agents.vector.policy import VECTOR_SETUP_V1
from src.agents.vector.ports import VectorContextUnavailable
from src.agents.vector.sufficiency import (
    RECOVERABLE,
    VectorMarketDataSufficiency,
    assess,
)
from src.core.clock import FixedClock
from src.markets.fake import fixture_history
from src.markets.history import (
    HistoryCoverage,
    MarketHistoryUnavailable,
    UnconfiguredHistorySource,
)
from src.markets.models import MarketIdentity
from src.orchestration.worker.capabilities import VectorCapabilities
from src.orchestration.worker.models import TaskFailureReport, WorkerFailureCategory
from src.reasoning.fake import DeterministicReasoningProvider
from tests.vector.conftest import (
    PAIR_ID,
    SPOT,
    StubCases,
    StubHistory,
    StubMarkets,
    StubTradeCase,
    history_for,
    market_identity,
    task_input,
)
from tests.vector.test_context import snapshot_for
from tests.vector.test_scenarios import lease_for, reply

MINIMUM = VECTOR_SETUP_V1.min_closed_bars


async def read(now, *, history=None, snapshot=None, market=None):
    reader = VectorContextReader(
        cases=StubCases(StubTradeCase(market or market_identity())),
        markets=StubMarkets(snapshot_for(now) if snapshot is None else snapshot),
        history=StubHistory(history_for(now) if history is None else history),
        clock=FixedClock(now),
        include_fixtures=True,
    )
    return await reader.setup_context(uuid4(), uuid4())


def verdict_for(now, history, identity=None):
    return assess(history, identity or market_identity(), now, VECTOR_SETUP_V1)


# ---------------------------------------------- A: the defect, reproduced


async def test_scenario_a_one_price_and_no_structure_is_not_enough(now):
    """The audit finding, pinned as a test.

    A market with a current price and no recorded structure supports no setup.
    Before this phase it supported four invented numbers.
    """
    with pytest.raises(VectorContextUnavailable) as error:
        await read(now, history=history_for(now, bars=0))
    assert error.value.reason_code == "MARKET_HISTORY_EMPTY"


async def test_scenario_a_the_model_is_never_asked_when_structure_is_missing(now):
    """No reasoning request is spent on a market that cannot support an answer."""

    class NoStructure:
        async def setup_context(self, trade_case_id, task_id):
            raise VectorContextUnavailable("MARKET_HISTORY_EMPTY")

    provider = DeterministicReasoningProvider.returning(reply(now))
    lease = lease_for(task_input(now), now)
    outcome = await VectorWorkerHandler(provider=provider).handle(
        lease, VectorCapabilities(lease=lease, context=NoStructure(), submit=object())
    )
    assert isinstance(outcome, TaskFailureReport)
    assert outcome.reason_code == "MARKET_HISTORY_EMPTY"
    assert outcome.category == WorkerFailureCategory.TRANSIENT
    assert provider.calls == []


async def test_an_unconfigured_history_source_fails_closed(now):
    """No provider wired is stated, never quietly treated as an empty market."""
    with pytest.raises(MarketHistoryUnavailable) as error:
        await UnconfiguredHistorySource().history(None, timeframe="hour", aggregate=1, bars=24)
    assert error.value.reason_code == "MARKET_HISTORY_SOURCE_NOT_CONFIGURED"
    # And retrying cannot configure one, so the attempt is not retried forever.
    assert (
        CONTEXT_FAILURES["MARKET_HISTORY_SOURCE_NOT_CONFIGURED"]
        == WorkerFailureCategory.CAPABILITY_DENIED
    )


def test_a_reader_without_a_configured_source_refuses_by_default():
    """The default is the refusing source, not a permissive one."""
    reader = VectorContextReader(cases=object(), markets=object())  # type: ignore[arg-type]
    assert isinstance(reader.history, UnconfiguredHistorySource)


# ------------------------------------------------- B: enough is enough


async def test_scenario_b_enough_fresh_bars_admits_the_market(now):
    context = await read(now, history=history_for(now, bars=MINIMUM))
    assert len(context.market.structure.bars) == MINIMUM
    assert verdict_for(now, history_for(now, bars=MINIMUM)) == (
        VectorMarketDataSufficiency.SUFFICIENT
    )


def test_the_minimum_is_a_data_quality_threshold_bound_to_the_horizon(now):
    """Twenty-four hourly bars behind a setup that lives at most four hours."""
    policy = VECTOR_SETUP_V1
    window = timedelta(seconds=3600 * policy.min_closed_bars)
    assert policy.history_timeframe == "hour" and policy.history_aggregate == 1
    assert window >= policy.max_setup_lifetime
    assert timedelta(hours=1) <= policy.max_setup_lifetime


# ----------------------------------------------- C, D, E: freshness


async def test_scenario_c_a_stale_series_stops_before_the_model(now):
    """Measured from when the newest bar closed, not from when we fetched it."""
    stale = history_for(now - timedelta(hours=5), bars=MINIMUM, fetched_at=now)
    assert stale.fetched_at == now
    with pytest.raises(VectorContextUnavailable) as error:
        await read(now, history=stale)
    assert error.value.reason_code == "MARKET_HISTORY_TOO_STALE"


async def test_scenario_d_a_fresh_price_does_not_rescue_a_stale_series(now):
    """Both must hold. One current number cannot vouch for absent structure."""
    fresh_snapshot = snapshot_for(now, minutes_ago=0)
    with pytest.raises(VectorContextUnavailable) as error:
        await read(
            now,
            snapshot=fresh_snapshot,
            history=history_for(now - timedelta(hours=6), bars=MINIMUM, fetched_at=now),
        )
    assert error.value.reason_code == "MARKET_HISTORY_TOO_STALE"


async def test_scenario_e_a_fresh_series_does_not_rescue_a_stale_price(now):
    """And the other way round: structure without a current price sets no level."""
    with pytest.raises(VectorContextUnavailable) as error:
        await read(
            now,
            snapshot=snapshot_for(now, minutes_ago=30),
            history=history_for(now, bars=MINIMUM),
        )
    assert error.value.reason_code == "MARKET_OBSERVATION_TOO_STALE"


def test_a_series_from_the_future_is_refused(now):
    future = fixture_history(
        market_identity(),
        newest_close=now + timedelta(hours=2),
        bars=MINIMUM,
        fetched_at=now + timedelta(hours=2),
    )
    assert verdict_for(now, future) == VectorMarketDataSufficiency.MARKET_HISTORY_IN_FUTURE


# ------------------------------------------ I, J: wrong unit, wrong market


def test_scenario_j_a_series_for_another_pool_is_refused(now):
    """Not "thin" — wrong. Retrying another pool's data would never converge."""
    other = history_for(now, bars=MINIMUM).model_copy(
        update={"pair_id": "robinhood:mainnet:contract_address:0x" + "ff" * 20}
    )
    assert verdict_for(now, other) == VectorMarketDataSufficiency.MARKET_HISTORY_IDENTITY_MISMATCH


def test_a_series_for_another_chain_is_refused(now):
    elsewhere = market_identity(chain="bsc")
    assert verdict_for(now, history_for(now, bars=MINIMUM), elsewhere) == (
        VectorMarketDataSufficiency.MARKET_HISTORY_IDENTITY_MISMATCH
    )


def test_a_series_for_another_base_asset_is_refused(now):
    """The pool can match while the priced token does not."""
    swapped = history_for(now, bars=MINIMUM).model_copy(
        update={
            "base_asset_id": "robinhood:mainnet:0x" + "b2" * 20,
            "quote_asset_id": "robinhood:mainnet:0x" + "a1" * 20,
        }
    )
    assert verdict_for(now, swapped) == VectorMarketDataSufficiency.MARKET_HISTORY_IDENTITY_MISMATCH


def test_scenario_i_a_series_in_another_orientation_is_refused(now):
    """The one failure that would look like working numbers all the way down."""
    inverted = history_for(now, bars=MINIMUM).model_copy(update={"price_basis": "USD_PER_QUOTE"})
    assert verdict_for(now, inverted) == (
        VectorMarketDataSufficiency.MARKET_HISTORY_PRICE_BASIS_MISMATCH
    )


def test_a_series_on_another_timeframe_is_refused(now):
    """The policy bound the timeframe to the horizon; a different one unbinds it.

    Daily bars must open at midnight, so this series is also stale — and the
    verdict is still the timeframe mismatch, because a series on the wrong
    timeframe is the wrong series rather than an old one.
    """
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daily = fixture_history(
        market_identity(), newest_close=midnight, bars=MINIMUM, timeframe="day", fetched_at=now
    )
    assert daily.timeframe == "day"
    assert verdict_for(now, daily) == VectorMarketDataSufficiency.MARKET_HISTORY_TIMEFRAME_MISMATCH


def test_identity_is_checked_before_length(now):
    """A wrong series must not be reported as a short one and retried."""
    wrong_and_short = history_for(now, bars=2).model_copy(
        update={"pair_id": "robinhood:mainnet:contract_address:0x" + "ff" * 20}
    )
    assert verdict_for(now, wrong_and_short) == (
        VectorMarketDataSufficiency.MARKET_HISTORY_IDENTITY_MISMATCH
    )


# --------------------------------------------- K, L: partial windows


async def test_scenario_k_a_window_below_the_minimum_is_insufficient(now):
    short = history_for(now, bars=MINIMUM - 1)
    assert short.coverage == HistoryCoverage.PARTIAL
    assert verdict_for(now, short) == VectorMarketDataSufficiency.MARKET_HISTORY_TOO_SHORT
    with pytest.raises(VectorContextUnavailable):
        await read(now, history=short)


async def test_scenario_l_a_partial_window_above_the_minimum_is_admitted(now):
    """A young pool with a day of trading is usable, and says it is partial."""
    partial = history_for(now, bars=MINIMUM)
    assert partial.coverage == HistoryCoverage.PARTIAL
    assert partial.requested_bars == VECTOR_SETUP_V1.history_bars
    context = await read(now, history=partial)
    assert context.market.structure.coverage == "PARTIAL"
    # And the evidence will say so rather than implying a full window.
    assert context.market.structure.requested_bars == VECTOR_SETUP_V1.history_bars


def test_a_whole_window_is_reported_as_complete(now):
    whole = history_for(now, bars=VECTOR_SETUP_V1.history_bars)
    assert whole.coverage == HistoryCoverage.COMPLETE
    assert whole.missing_intervals == 0


def test_gaps_are_counted_rather_than_filled(now):
    """An interval nobody traded in is absent, and the absence is visible."""
    gapped = history_for(now, bars=40, skip=frozenset({5, 6, 7}))
    assert len(gapped.bars) == 37
    assert gapped.missing_intervals == 3
    assert gapped.coverage == HistoryCoverage.PARTIAL


def test_a_window_that_is_mostly_gaps_is_not_structure(now):
    holes = frozenset(range(2, 40, 2))
    swiss = history_for(now, bars=44, skip=holes)
    assert len(swiss.bars) >= VECTOR_SETUP_V1.min_closed_bars
    assert verdict_for(now, swiss) == VectorMarketDataSufficiency.MARKET_HISTORY_TOO_GAPPED


# ---------------------------------------------- M, N: the input digest


async def test_scenario_m_the_same_bars_refetched_later_hash_the_same(now):
    """Retrieval time is excluded, so looking twice is not new information."""
    series = history_for(now, bars=MINIMUM)
    later = series.model_copy(update={"fetched_at": now + timedelta(minutes=3)})
    snapshot = snapshot_for(now)
    first = await read(now, history=series, snapshot=snapshot)
    again = await read(now, history=later, snapshot=snapshot)
    assert series.fetched_at != later.fetched_at
    assert vector_input_digest(first) == vector_input_digest(again)


async def test_scenario_n_one_changed_bar_changes_the_digest(now):
    """Whatever the model was shown is exactly what the fingerprint covers."""
    series = history_for(now, bars=MINIMUM)
    moved = series.model_copy(
        update={
            "bars": (
                *series.bars[:-1],
                series.bars[-1].model_copy(update={"high": series.bars[-1].high * Decimal("1.5")}),
            )
        }
    )
    snapshot = snapshot_for(now)
    assert vector_input_digest(await read(now, history=series, snapshot=snapshot)) != (
        vector_input_digest(await read(now, history=moved, snapshot=snapshot))
    )


async def test_a_shorter_window_changes_the_digest(now):
    snapshot = snapshot_for(now)
    assert vector_input_digest(
        await read(now, history=history_for(now, bars=MINIMUM), snapshot=snapshot)
    ) != (
        vector_input_digest(
            await read(now, history=history_for(now, bars=MINIMUM + 1), snapshot=snapshot)
        )
    )


def test_a_changed_policy_version_changes_the_digest(now):
    from dataclasses import replace

    from src.agents.vector.context import setup_document

    context = task_input(now)
    other = context.model_copy(
        update={"policy_version": replace(VECTOR_SETUP_V1, version="vector-setup-v9").version}
    )
    assert setup_document(context) != setup_document(other)


# ------------------------------------------------------- categorisation


@pytest.mark.parametrize("verdict", sorted(RECOVERABLE))
def test_a_market_that_may_improve_is_retried(verdict):
    assert CONTEXT_FAILURES[verdict.value] == WorkerFailureCategory.TRANSIENT


@pytest.mark.parametrize(
    "verdict",
    sorted(
        set(VectorMarketDataSufficiency) - RECOVERABLE - {VectorMarketDataSufficiency.SUFFICIENT}
    ),
)
def test_a_wiring_fault_is_not_retried_as_if_it_were_weather(verdict):
    assert CONTEXT_FAILURES[verdict.value] == WorkerFailureCategory.INTERNAL


def test_every_verdict_has_a_declared_category():
    """No sufficiency failure may fall through to a default categorisation."""
    for verdict in VectorMarketDataSufficiency:
        if verdict != VectorMarketDataSufficiency.SUFFICIENT:
            assert verdict.value in CONTEXT_FAILURES


def test_the_model_has_no_say_in_whether_its_input_was_enough(now):
    """Sufficiency is arithmetic. Nothing the model returns can reach it."""
    from src.agents.vector.models import VectorSetupProposal

    for forbidden in ("sufficient", "confidence", "data_quality", "needs_more_data"):
        assert forbidden not in VectorSetupProposal.model_fields
    identity = MarketIdentity.model_validate(market_identity().model_dump())
    assert assess(history_for(now, bars=1), identity, now) == (
        VectorMarketDataSufficiency.MARKET_HISTORY_TOO_SHORT
    )


def test_the_series_is_anchored_to_one_pool_and_one_unit(now):
    series = history_for(now, bars=MINIMUM)
    assert series.pair_id == PAIR_ID
    assert series.price_basis == "USD_PER_BASE_UNIT"
    assert series.range_low < SPOT < series.range_high


# ---------------------------------------------- the timeframe/horizon binding


def test_a_bar_longer_than_the_longest_setup_refuses_to_exist():
    """Thirty daily bars cannot support a four-hour setup: the bar outlives it."""
    from dataclasses import replace

    with pytest.raises(ValueError, match="longer than the longest setup"):
        replace(
            VECTOR_SETUP_V1,
            history_timeframe="day",
            history_aggregate=1,
            max_history_age=timedelta(days=2),
            min_closed_bars=24,
        )


def test_a_window_shorter_than_the_longest_setup_refuses_to_exist():
    """Five one-minute bars cannot support a four-hour thesis."""
    from dataclasses import replace

    with pytest.raises(ValueError, match="must cover the longest setup"):
        replace(
            VECTOR_SETUP_V1,
            history_timeframe="minute",
            history_aggregate=1,
            max_history_age=timedelta(minutes=5),
            min_closed_bars=5,
            history_bars=10,
        )


def test_a_negative_range_extension_refuses_to_exist():
    from dataclasses import replace

    with pytest.raises(ValueError):
        replace(VECTOR_SETUP_V1, range_extension=Decimal(-1))


def test_the_shipped_policy_satisfies_its_own_binding():
    interval = timedelta(hours=1)
    assert interval <= VECTOR_SETUP_V1.max_setup_lifetime
    assert VECTOR_SETUP_V1.min_closed_bars * interval >= VECTOR_SETUP_V1.max_setup_lifetime


# ------------------------------------------- the structure view's own rules


def test_a_structure_view_with_an_inverted_window_is_refused(now):
    from src.agents.vector.models import VectorMarketStructure

    view = task_input(now).market.structure
    payload = view.model_dump() | {"window_end": view.window_start - timedelta(hours=1)}
    with pytest.raises(ValueError):
        VectorMarketStructure.model_validate(payload)


def test_a_structure_view_must_carry_at_least_one_bar(now):
    """There is no such thing as a grounded setup drawn from an empty window."""
    from src.agents.vector.models import VectorMarketStructure

    view = task_input(now).market.structure
    with pytest.raises(ValueError):
        VectorMarketStructure.model_validate(view.model_dump() | {"bars": []})


def test_a_structure_view_cannot_hold_more_bars_than_were_requested(now):
    from src.agents.vector.models import VectorMarketStructure

    view = task_input(now).market.structure
    with pytest.raises(ValueError):
        VectorMarketStructure.model_validate(view.model_dump() | {"requested_bars": 1})


def test_a_structure_view_cannot_carry_an_incoherent_bar(now):
    from src.agents.vector.models import VectorMarketStructure

    view = task_input(now).market.structure
    payload = view.model_dump()
    payload["bars"][2]["low"] = payload["bars"][2]["high"] * Decimal(2)
    with pytest.raises(ValueError):
        VectorMarketStructure.model_validate(payload)


def test_a_structure_view_cannot_carry_unordered_bars(now):
    from src.agents.vector.models import VectorMarketStructure

    view = task_input(now).market.structure
    payload = view.model_dump()
    payload["bars"] = list(reversed(payload["bars"]))
    with pytest.raises(ValueError):
        VectorMarketStructure.model_validate(payload)


# ------------------------------------------------------ source failures


async def test_a_source_that_cannot_answer_ends_the_attempt_safely(now):
    """A provider failure becomes a safe reason code, never an escaping exception."""
    reader = VectorContextReader(
        cases=StubCases(StubTradeCase(market_identity())),
        markets=StubMarkets(snapshot_for(now)),
        history=StubHistory(MarketHistoryUnavailable("MARKET_HISTORY_SOURCE_NOT_CONFIGURED")),
        clock=FixedClock(now),
        include_fixtures=True,
    )
    with pytest.raises(VectorContextUnavailable) as error:
        await reader.setup_context(uuid4(), uuid4())
    assert error.value.reason_code == "MARKET_HISTORY_SOURCE_NOT_CONFIGURED"


# ------------------------------------- the open bar never fills the minimum


async def test_an_open_bar_is_not_counted_toward_the_minimum(now):
    """23 closed plus one still forming is 23, and 23 is not enough.

    Counting the open interval to reach the threshold would mean the whole
    sufficiency gate could be satisfied by a bar whose high and low are not
    finished being made.
    """
    short = history_for(now, bars=MINIMUM - 1)
    assert len(short.bars) == MINIMUM - 1
    assert verdict_for(now, short) == VectorMarketDataSufficiency.MARKET_HISTORY_TOO_SHORT
    with pytest.raises(VectorContextUnavailable) as error:
        await read(now, history=short)
    assert error.value.reason_code == "MARKET_HISTORY_TOO_SHORT"


async def test_exactly_the_minimum_of_closed_bars_is_admitted(now):
    context = await read(now, history=history_for(now, bars=MINIMUM))
    assert len(context.market.structure.bars) == MINIMUM


async def test_the_supplied_window_is_explicit_rather_than_implied(now):
    """48 is requested, 24 is required, and the model is told what it actually got.

    The request size is an implementation detail — one extra bar covers the
    forming interval, and a wider window absorbs gaps without failing admission.
    It must never read as a claimed analysis horizon, so the count, the window
    bounds and the requested size are all present and distinct.
    """
    structure = (await read(now, history=history_for(now, bars=30))).market.structure
    assert len(structure.bars) == 30
    assert structure.requested_bars == VECTOR_SETUP_V1.history_bars == 48
    assert VECTOR_SETUP_V1.min_closed_bars == 24
    assert structure.window_start < structure.window_end
    span = (structure.window_end - structure.window_start).total_seconds()
    assert span == structure.interval_seconds * len(structure.bars)


# ------------------------------------------- provider failure classification


@pytest.mark.parametrize(
    ("reason", "category"),
    [
        ("MARKET_HISTORY_PROVIDER_RATE_LIMITED", WorkerFailureCategory.TRANSIENT),
        ("MARKET_HISTORY_PROVIDER_UNAVAILABLE", WorkerFailureCategory.TRANSIENT),
        ("MARKET_HISTORY_REQUEST_BUDGET_EXHAUSTED", WorkerFailureCategory.TRANSIENT),
        ("MARKET_HISTORY_PROVIDER_CONTRACT", WorkerFailureCategory.INTERNAL),
        ("MARKET_HISTORY_PROVIDER_IDENTITY", WorkerFailureCategory.INTERNAL),
        ("MARKET_HISTORY_PROVIDER_REJECTED", WorkerFailureCategory.INTERNAL),
        ("MARKET_HISTORY_NETWORK_UNSUPPORTED", WorkerFailureCategory.INTERNAL),
        ("MARKET_HISTORY_PROVIDER_NOT_AUTHORIZED", WorkerFailureCategory.CAPABILITY_DENIED),
    ],
)
async def test_a_provider_failure_is_classified_rather_than_escaping(now, reason, category):
    """Every provider condition arrives as a typed outcome, never as a handler bug.

    Before this, a rate limit escaped the context reader untyped and landed in
    the runner's catch-all as INTERNAL/HANDLER_ERROR — indistinguishable from a
    defect in our own code.
    """
    assert CONTEXT_FAILURES[reason] == category

    class Failing:
        async def setup_context(self, trade_case_id, task_id):
            raise VectorContextUnavailable(reason)

    provider = DeterministicReasoningProvider.returning(reply(now))
    lease = lease_for(task_input(now), now)
    outcome = await VectorWorkerHandler(provider=provider).handle(
        lease, VectorCapabilities(lease=lease, context=Failing(), submit=object())
    )
    assert isinstance(outcome, TaskFailureReport)
    assert outcome.reason_code == reason
    assert outcome.category == category
    # And no reasoning request was spent on it.
    assert provider.calls == []


async def test_a_rate_limited_provider_produces_no_setup_at_all(now):
    """Not an empty market, not a short window: no answer, and no evidence."""
    reader = VectorContextReader(
        cases=StubCases(StubTradeCase(market_identity())),
        markets=StubMarkets(snapshot_for(now)),
        history=StubHistory(MarketHistoryUnavailable("MARKET_HISTORY_PROVIDER_RATE_LIMITED")),
        clock=FixedClock(now),
        include_fixtures=True,
    )
    with pytest.raises(VectorContextUnavailable) as error:
        await reader.setup_context(uuid4(), uuid4())
    assert error.value.reason_code == "MARKET_HISTORY_PROVIDER_RATE_LIMITED"
    assert CONTEXT_FAILURES[error.value.reason_code] == WorkerFailureCategory.TRANSIENT


def test_a_rate_limit_is_not_any_kind_of_statement_about_the_market():
    """The codes are kept apart so downstream can never conflate them."""
    limited = "MARKET_HISTORY_PROVIDER_RATE_LIMITED"
    for market_fact in (
        VectorMarketDataSufficiency.MARKET_HISTORY_EMPTY,
        VectorMarketDataSufficiency.MARKET_HISTORY_TOO_SHORT,
        VectorMarketDataSufficiency.MARKET_HISTORY_TOO_GAPPED,
    ):
        assert limited != market_fact.value
    assert limited not in {item.value for item in VectorMarketDataSufficiency}
