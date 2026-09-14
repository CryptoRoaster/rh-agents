"""Two completeness defects an independent review reproduced against 215b56e.

Both let something reach a risk reading that has no business being there: an
advisory opinion arriving as a safety blocker, and a stale fact arriving as a
present one.
"""

from dataclasses import replace
from datetime import timedelta

import pytest

from src.orchestration.riskdata.models import RiskDataGapCode, RiskFactKind
from src.orchestration.riskdata.policy import RISK_DATA_V1
from src.orchestration.workflow.models import EvidenceType
from src.orchestration.workflow.policy import TRADE_CASE_V1
from tests.riskdata.conftest import (
    RecordedMarkets,
    build_reader,
    onchain_payload,
    prepare_case,
    record_sentiment,
    record_synthesis,
    recorded_snapshot,
)


def gap(reading, kind):
    return next((item for item in reading.gaps if item.kind is kind), None)


def fact(reading, kind):
    return next((item for item in reading.facts if item.kind is kind), None)


def shape(reading):
    """What a later risk step would actually act on."""
    return (
        sorted((item.kind.value, item.code.value) for item in reading.gaps),
        sorted(item.code for item in reading.blockers),
    )


# =================================================== 1. advisory is not a blocker


async def test_a_negative_synthesis_changes_no_risk_blocker(worker_db, now, trace):
    """The reproduction: same canonical safety evidence, two advisory readings.

    FUSE's acceptance reaches `BLOCKED` from its own reading of evidence that is
    entirely unchanged. Collecting that as a risk blocker handed the advisory
    layer, indirectly, the authority it is kept out of the risk-input digest to
    deny it.
    """
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    trade_case = await prepare_case(reader.cases, now, trace)
    without = await reader.readiness(trade_case.id)

    envelope = await record_synthesis(reader.cases, trade_case, now, blocking=True)
    with_advisory = await reader.readiness(trade_case.id)

    assert envelope.payload.acceptance().value == "BLOCKED"
    assert shape(with_advisory) == shape(without)
    assert "FUSE_EVIDENCE_BLOCKED" not in {item.code for item in with_advisory.blockers}
    assert with_advisory.complete == without.complete is True


async def test_an_agreeable_synthesis_changes_nothing_either(worker_db, now, trace):
    """Advisory in both directions: it cannot help and it cannot hurt."""
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    trade_case = await prepare_case(reader.cases, now, trace)
    without = await reader.readiness(trade_case.id)

    await record_synthesis(reader.cases, trade_case, now, blocking=False)

    assert shape(await reader.readiness(trade_case.id)) == shape(without)


async def test_sentiment_has_no_indirect_risk_effect(worker_db, now, trace):
    """SIGNAL gates the workflow without binding risk, by an explicit decision."""
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    trade_case = await prepare_case(reader.cases, now, trace)
    without = await reader.readiness(trade_case.id)

    await record_sentiment(reader.cases, trade_case, now, assessment="NEGATIVE")

    assert shape(await reader.readiness(trade_case.id)) == shape(without)


def test_the_blocker_scope_is_the_workflow_s_own_safety_table():
    """One authority on which evidence is safety-critical, not two.

    The reader carries the workflow policy rather than a list of its own, so a
    requirement that changes there cannot leave a second copy behind here.
    """
    from src.orchestration.riskdata.context import RiskDataReader

    assert RiskDataReader.workflow is TRADE_CASE_V1
    safety = TRADE_CASE_V1.safety_types
    assert EvidenceType.SYNTHESIS not in safety
    assert EvidenceType.SENTIMENT not in safety
    assert EvidenceType.DISCOVERY not in safety
    assert {
        EvidenceType.ONCHAIN,
        EvidenceType.TRADE_SETUP,
        EvidenceType.TRIGGER,
        EvidenceType.LIQUIDITY_EXECUTION,
    } == safety


async def test_a_narrowed_safety_table_narrows_the_blockers_with_it(worker_db, now, trace):
    """Proof the scope is read rather than restated.

    Removing ATLAS from the safety requirements silences its blocker, which is
    only possible if the reader consults that table instead of its own copy.
    """
    _, sessions = worker_db
    payload = onchain_payload(now, contract="FAIL", blockers=("CONTRACT_CODE_ABSENT",))
    reader = build_reader(sessions, now)
    trade_case = await prepare_case(reader.cases, now, trace, onchain=payload)
    assert "ATLAS_EVIDENCE_BLOCKED" in {
        item.code for item in (await reader.readiness(trade_case.id)).blockers
    }

    narrowed = replace(
        TRADE_CASE_V1,
        requirements=tuple(
            item
            if item.evidence_type is not EvidenceType.ONCHAIN
            else replace(item, safety_critical=False)
            for item in TRADE_CASE_V1.requirements
        ),
    )
    quiet = build_reader(sessions, now, feed=reader.markets, workflow=narrowed)
    assert "ATLAS_EVIDENCE_BLOCKED" not in {
        item.code for item in (await quiet.readiness(trade_case.id)).blockers
    }


async def test_direct_safety_blockers_survive_the_narrowing(worker_db, now, trace):
    """What must still get through: a measured on-chain violation.

    Both the envelope's blocked acceptance and ATLAS's own blocker codes.
    """
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    payload = onchain_payload(
        now, contract="FAIL", blockers=("CONTRACT_CODE_ABSENT", "TOTAL_SUPPLY_ZERO")
    )
    trade_case = await prepare_case(reader.cases, now, trace, onchain=payload)
    await record_synthesis(reader.cases, trade_case, now, blocking=True)

    codes = {item.code for item in (await reader.readiness(trade_case.id)).blockers}

    assert {"ATLAS_EVIDENCE_BLOCKED", "CONTRACT_CODE_ABSENT", "TOTAL_SUPPLY_ZERO"} <= codes
    assert "FUSE_EVIDENCE_BLOCKED" not in codes


async def test_a_data_gap_still_never_hides_a_blocker(worker_db, now, trace):
    """Incompletely measured and known dangerous remain two separate statements."""
    _, sessions = worker_db
    reader = build_reader(sessions, now, feed=RecordedMarkets(None))
    payload = onchain_payload(now, contract="FAIL", blockers=("CONTRACT_CODE_ABSENT",))
    trade_case = await prepare_case(reader.cases, now, trace, onchain=payload)
    await record_synthesis(reader.cases, trade_case, now, blocking=True)

    reading = await reader.readiness(trade_case.id)

    assert not reading.complete
    assert reading.gaps
    assert "CONTRACT_CODE_ABSENT" in {item.code for item in reading.blockers}
    assert fact(reading, RiskFactKind.TOKEN_TRADABILITY) is not None


# ============================================ 2. token metadata freshness


async def test_day_old_metadata_inside_a_fresh_snapshot_is_stale(worker_db, now, trace):
    """The reproduction, on a valid market model.

    Price and liquidity observed thirty seconds ago, the base asset's symbol and
    decimals a day earlier — a shape the recorder can legitimately produce,
    because a nested observation may be older than its parent. Reading the
    snapshot's own time made the older fact look as fresh as the newer one it
    travelled with.
    """
    _, sessions = worker_db
    feed = RecordedMarkets(recorded_snapshot(now, metadata_age=timedelta(days=1)))
    reader = build_reader(sessions, now, feed=feed)
    trade_case = await prepare_case(reader.cases, now, trace)

    reading = await reader.readiness(trade_case.id)

    assert not reading.complete
    assert gap(reading, RiskFactKind.TOKEN_METADATA).code is RiskDataGapCode.STALE
    # The rest of the snapshot is unaffected: only the aged fact is withheld.
    assert fact(reading, RiskFactKind.REFERENCE_PRICE) is not None
    assert fact(reading, RiskFactKind.LIQUIDITY_DEPTH) is not None


@pytest.mark.parametrize(
    ("metadata_age", "expected"),
    [
        (timedelta(seconds=89), None),
        (timedelta(seconds=89, microseconds=999999), None),
        (RISK_DATA_V1.max_token_metadata_age, RiskDataGapCode.STALE),
        (timedelta(seconds=91), RiskDataGapCode.STALE),
    ],
)
async def test_metadata_freshness_at_and_around_the_boundary(
    worker_db, now, trace, metadata_age, expected
):
    """Half-open at the far edge, like every other validity in this system."""
    _, sessions = worker_db
    feed = RecordedMarkets(recorded_snapshot(now, age=metadata_age, metadata_age=metadata_age))
    reader = build_reader(sessions, now, feed=feed)
    trade_case = await prepare_case(reader.cases, now, trace)

    reading = await reader.readiness(trade_case.id)

    found = gap(reading, RiskFactKind.TOKEN_METADATA)
    assert (None if found is None else found.code) is expected


async def test_metadata_is_judged_on_its_own_source_time(worker_db, now, trace):
    """Not the snapshot's, not the fetch's, not the reader's.

    The same metadata instant is stale inside a snapshot observed a moment ago
    and stale inside one observed at the same time as the metadata — because the
    enclosing observation never enters the judgement at all.
    """
    _, sessions = worker_db
    aged = timedelta(seconds=120)
    for snapshot_age in (timedelta(seconds=1), timedelta(seconds=60), aged):
        feed = RecordedMarkets(recorded_snapshot(now, age=snapshot_age, metadata_age=aged))
        reader = build_reader(sessions, now, feed=feed)
        trade_case = await prepare_case(
            reader.cases, now, trace, key=f"meta-{int(snapshot_age.total_seconds())}"
        )
        reading = await reader.readiness(trade_case.id)
        assert gap(reading, RiskFactKind.TOKEN_METADATA).code is RiskDataGapCode.STALE


async def test_metadata_validity_enters_the_reading_s_validity(worker_db, now, trace):
    """A reading is only as current as the soonest thing in it left to lapse."""
    _, sessions = worker_db
    feed = RecordedMarkets(
        recorded_snapshot(now, age=timedelta(seconds=1), metadata_age=timedelta(seconds=80))
    )
    reader = build_reader(sessions, now, feed=feed)
    trade_case = await prepare_case(reader.cases, now, trace)

    reading = await reader.readiness(trade_case.id)

    metadata = fact(reading, RiskFactKind.TOKEN_METADATA)
    assert metadata.valid_until == metadata.observed_at + RISK_DATA_V1.max_token_metadata_age
    assert reading.valid_until == metadata.valid_until
    assert reading.is_current_at(now)
    assert not reading.is_current_at(reading.valid_until)


async def test_metadata_observed_in_the_future_is_refused(worker_db, now, trace):
    _, sessions = worker_db
    ahead = timedelta(seconds=-5)
    feed = RecordedMarkets(recorded_snapshot(now, age=ahead, metadata_age=ahead))
    reader = build_reader(sessions, now, feed=feed)
    trade_case = await prepare_case(reader.cases, now, trace)

    reading = await reader.readiness(trade_case.id)

    assert gap(reading, RiskFactKind.TOKEN_METADATA).code is RiskDataGapCode.OBSERVED_IN_THE_FUTURE


async def test_a_fully_fresh_case_is_still_complete(worker_db, now, trace):
    """The control. The new check refuses stale data, not ordinary data."""
    _, sessions = worker_db
    reader = build_reader(sessions, now)
    trade_case = await prepare_case(reader.cases, now, trace)

    reading = await reader.readiness(trade_case.id)

    assert reading.complete
    assert fact(reading, RiskFactKind.TOKEN_METADATA).valid_until is not None


def test_the_metadata_bound_is_chosen_rather_than_inherited():
    """A separate field, because SENTINEL checks the token snapshot separately.

    It is necessary and not sufficient: SENTINEL applies its own configurable
    tolerance when it evaluates, and that one is tighter by default, so a
    complete reading here is not a promise that a later evaluation will accept
    the same data.
    """
    from dataclasses import fields

    from src.core.models import RiskLimits

    names = {item.name for item in fields(type(RISK_DATA_V1))}
    assert "max_token_metadata_age" in names
    assert RISK_DATA_V1.max_token_metadata_age > timedelta(
        seconds=RiskLimits().max_snapshot_age_seconds
    )
    with pytest.raises(ValueError):
        replace(RISK_DATA_V1, max_token_metadata_age=timedelta(0))
