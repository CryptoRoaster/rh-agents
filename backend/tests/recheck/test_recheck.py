"""A market refused only by the liquidity/FDV gate may be asked about once more.

Never automatically, never with anything inherited, and never past its bounds.
"""

import asyncio
import os
from datetime import timedelta
from itertools import combinations

import pytest
from sqlalchemy import select

from src.core.clock import FixedClock
from src.core.models import RiskLimits, RiskOutcome
from src.data.tables import TradeCaseEventRow, TradeCaseRiskBindingRow, TradeCaseRow
from src.orchestration.commander.intake import active_case_exists, market_barring
from src.orchestration.entryhalt.service import EntryHaltService
from src.orchestration.recheck.models import LIQ_FDV_RECHECK_V1, RecheckRefusal
from src.orchestration.recheck.service import RecheckUnavailable, recheck_key
from src.orchestration.workflow.models import TradeCaseStatus
from src.orchestration.workflow.service import TradeCaseService
from src.risk.authorization import RiskAuthorization
from tests.recheck.conftest import (
    HEAVY_SUPPLY,
    LATER,
    PAIR_ID,
    THIN_SUPPLY,
    build_recheck,
    build_service,
    evidence_for,
    fresh_onchain,
    history,
    market_cases,
    onchain_with,
    rejected_by_sentinel,
    rejected_with,
    set_account,
)

GATE = sorted(LIQ_FDV_RECHECK_V1.eligible_codes)


# ------------------------------------------------------------ eligibility


@pytest.mark.parametrize(
    "codes",
    [(code,) for code in GATE] + [pair for pair in combinations(GATE, 2)] + [tuple(GATE)],
)
async def test_a_gate_only_rejection_opens_exactly_one_successor(risk_db, now, trace, codes):
    _, sessions = risk_db
    rejected = await rejected_with(sessions, now, trace, key="gate", codes=codes)

    opened = await build_recheck(sessions, now + LATER).open_recheck(rejected.id)

    assert opened.kind == "recheck_opened", getattr(opened, "reason", None)
    assert opened.replayed is False
    assert opened.authorizes_execution is False
    assert opened.reason_codes == tuple(sorted(codes))
    assert opened.policy_version == "LIQ_FDV_RECHECK_V1"
    assert opened.trade_case_id != rejected.id
    again = await build_recheck(sessions, now + LATER + timedelta(minutes=1)).open_recheck(
        rejected.id
    )
    assert again.kind == "recheck_opened"
    assert again.replayed is True
    assert again.trade_case_id == opened.trade_case_id
    assert await market_cases(sessions) == 2


@pytest.mark.parametrize(
    "codes",
    [
        ("INSUFFICIENT_LIQUIDITY",),
        ("HOLDER_CONCENTRATION_LIMIT",),
        ("LP_CONTROL_UNKNOWN",),
        ("KILL_SWITCH",),
        ("FDV_DECIMALS_MISMATCH",),
        ("STALE_OR_FUTURE_SAFETY_DATA",),
        # One code outside the gate is enough to keep the market barred.
        ("LIQUIDITY_FDV_RATIO_LIMIT", "HOLDER_CONCENTRATION_LIMIT"),
        ("FDV_UNKNOWN", "HOLDERS_UNKNOWN"),
        ("LIQUIDITY_FDV_RATIO_UNVERIFIED", "FDV_DECIMALS_MISMATCH"),
    ],
)
async def test_any_other_code_keeps_the_market_barred(risk_db, now, trace, codes):
    _, sessions = risk_db
    rejected = await rejected_with(sessions, now, trace, key="other", codes=codes)

    refused = await build_recheck(sessions, now + LATER).open_recheck(rejected.id)

    assert refused.kind == "recheck_refused"
    assert refused.reason is RecheckRefusal.REASON_NOT_ELIGIBLE
    assert refused.detail in codes
    assert refused.detail not in LIQ_FDV_RECHECK_V1.eligible_codes
    assert await market_cases(sessions) == 1


async def test_a_system_pause_is_not_a_gate_rejection(risk_db, now, trace):
    _, sessions = risk_db
    rejected = await rejected_with(
        sessions,
        now,
        trace,
        key="paused",
        codes=("LIQUIDITY_FDV_RATIO_LIMIT",),
        outcome=RiskOutcome.PAUSE_SYSTEM,
    )

    refused = await build_recheck(sessions, now + LATER).open_recheck(rejected.id)

    assert refused.reason is RecheckRefusal.PREDECESSOR_NOT_REJECT
    assert refused.detail == "PAUSE_SYSTEM"


async def test_only_a_rejected_case_is_a_predecessor(risk_db, now, trace):
    _, sessions = risk_db
    service = build_service(sessions, now)
    from tests.recheck.conftest import ready_case

    case = await ready_case(service.cases, now, trace, key="approved")
    result = await service.request_risk_evaluation(case.id, request_key="approved-risk")
    assert result.authorization is RiskAuthorization.APPROVED

    refused = await build_recheck(sessions, now + LATER).open_recheck(case.id)

    assert refused.reason is RecheckRefusal.PREDECESSOR_NOT_RISK_REJECTED
    assert refused.detail == "RISK_APPROVED"


async def test_an_unknown_predecessor_is_refused(risk_db, now):
    from uuid import uuid4

    _, sessions = risk_db
    refused = await build_recheck(sessions, now).open_recheck(uuid4())
    assert refused.reason is RecheckRefusal.PREDECESSOR_NOT_FOUND


async def test_a_rejection_without_its_binding_is_not_placed(risk_db, now, trace):
    """A status the decision cannot be found for is not re-checked on trust."""
    _, sessions = risk_db
    rejected = await rejected_with(
        sessions, now, trace, key="unbound", codes=("LIQUIDITY_FDV_RATIO_LIMIT",)
    )
    async with sessions.begin() as session:
        row = await session.get(TradeCaseRow, rejected.id)
        row.risk_input_digest = "0" * 64

    refused = await build_recheck(sessions, now + LATER).open_recheck(rejected.id)

    assert refused.reason is RecheckRefusal.PREDECESSOR_DECISION_UNREADABLE


# ------------------------------------------------------------ time and count


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        (timedelta(minutes=30) - timedelta(microseconds=1), RecheckRefusal.RECHECK_TOO_EARLY),
        (timedelta(minutes=30), None),
        (timedelta(hours=24), None),
        (timedelta(hours=24, microseconds=1), RecheckRefusal.RECHECK_WINDOW_CLOSED),
    ],
)
async def test_the_window_opens_and_closes_exactly(risk_db, now, trace, after, expected):
    _, sessions = risk_db
    rejected = await rejected_with(
        sessions, now, trace, key="window", codes=("LIQUIDITY_FDV_RATIO_LIMIT",)
    )

    result = await build_recheck(sessions, now + after).open_recheck(rejected.id)

    if expected is None:
        assert result.kind == "recheck_opened"
    else:
        assert result.kind == "recheck_refused"
        assert result.reason is expected
        assert await market_cases(sessions) == 1


async def reject_successor(sessions, successor_id, at, codes):
    """Carry a successor through fresh evidence to another gate rejection."""
    from tests.recheck.conftest import decision

    cases = TradeCaseService(sessions, clock=FixedClock(at))
    ready = await evidence_for(cases, successor_id, at, onchain=fresh_onchain(at))
    assert ready.status is TradeCaseStatus.READY_FOR_RISK
    await cases.record_risk_decision(
        successor_id, decision(ready, at, codes=codes), risk_input_digest=ready.risk_input_digest
    )
    return await cases.get_trade_case(successor_id)


async def test_a_market_gets_at_most_two_successors(risk_db, now, trace):
    _, sessions = risk_db
    codes = ("LIQUIDITY_FDV_RATIO_LIMIT",)
    first = await rejected_with(sessions, now, trace, key="cap", codes=codes)

    at = now + LATER
    one = await build_recheck(sessions, at).open_recheck(first.id)
    assert one.kind == "recheck_opened"
    second = await reject_successor(sessions, one.trade_case_id, at, codes)
    assert second.status is TradeCaseStatus.RISK_REJECTED

    at += LATER
    two = await build_recheck(sessions, at).open_recheck(second.id)
    assert two.kind == "recheck_opened", getattr(two, "reason", None)
    third = await reject_successor(sessions, two.trade_case_id, at, codes)

    at += LATER
    refused = await build_recheck(sessions, at).open_recheck(third.id)
    assert refused.reason is RecheckRefusal.RECHECK_LIMIT_REACHED
    assert await market_cases(sessions) == 3


async def test_only_the_end_of_a_history_is_rechecked(risk_db, now, trace):
    """The first rejection, once succeeded, is not a second way in."""
    _, sessions = risk_db
    codes = ("LIQUIDITY_FDV_RATIO_LIMIT",)
    first = await rejected_with(sessions, now, trace, key="end", codes=codes)
    at = now + LATER
    one = await build_recheck(sessions, at).open_recheck(first.id)
    await reject_successor(sessions, one.trade_case_id, at, codes)

    # Replaying the first predecessor returns its one successor, nothing new.
    replay = await build_recheck(sessions, at + LATER).open_recheck(first.id)
    assert replay.replayed is True
    assert replay.trade_case_id == one.trade_case_id
    assert await market_cases(sessions) == 2


async def test_a_successor_that_lapsed_is_not_retried(risk_db, now, trace):
    """An abandoned successor ends the history in EXPIRED, which nothing re-checks."""
    _, sessions = risk_db
    first = await rejected_with(
        sessions, now, trace, key="lapsed", codes=("LIQUIDITY_FDV_RATIO_LIMIT",)
    )
    at = now + LATER
    one = await build_recheck(sessions, at).open_recheck(first.id)

    late = at + LIQ_FDV_RECHECK_V1.case_lifetime + timedelta(minutes=1)
    lapsed = await TradeCaseService(sessions, clock=FixedClock(late)).evaluate_trade_case(
        one.trade_case_id
    )
    assert lapsed.status is TradeCaseStatus.EXPIRED

    again = await build_recheck(sessions, late).open_recheck(one.trade_case_id)
    assert again.reason is RecheckRefusal.PREDECESSOR_NOT_RISK_REJECTED
    first_again = await build_recheck(sessions, late).open_recheck(first.id)
    assert first_again.replayed is True
    assert await market_cases(sessions) == 2


async def test_an_ordinary_rejection_elsewhere_in_the_history_bars_it(risk_db, now, trace):
    """Defence in depth: a history holding a real verdict is never continued.

    Intake would never open the second case; it is opened directly here, as a
    record no path is meant to produce, to show the history is read in full.
    """
    _, sessions = risk_db
    await rejected_with(sessions, now, trace, key="verdict", codes=("HOLDER_CONCENTRATION_LIMIT",))
    later = now + timedelta(minutes=1)
    gate = await rejected_with(
        sessions, later, trace, key="gate-after", codes=("LIQUIDITY_FDV_RATIO_LIMIT",)
    )

    refused = await build_recheck(sessions, later + LATER).open_recheck(gate.id)

    assert refused.reason is RecheckRefusal.MARKET_REJECTED_OTHERWISE
    assert await market_cases(sessions) == 2


# ------------------------------------------------------------ the stops


@pytest.mark.parametrize(
    ("overrides", "account", "expected"),
    [
        ({"kill_switch": True}, {}, RecheckRefusal.KILL_SWITCH_ENGAGED),
        ({"limits": RiskLimits(kill_switch=True)}, {}, RecheckRefusal.KILL_SWITCH_ENGAGED),
        ({"pause": None}, {}, RecheckRefusal.SYSTEM_STOP_UNREADABLE),
        ({}, {"paused": True}, RecheckRefusal.SYSTEM_PAUSED),
        ({}, "halt", RecheckRefusal.ENTRY_HALTED),
    ],
)
async def test_every_stop_is_honoured(risk_db, now, trace, overrides, account, expected):
    _, sessions = risk_db
    rejected = await rejected_with(
        sessions, now, trace, key="stops", codes=("LIQUIDITY_FDV_RATIO_LIMIT",)
    )
    if account == "halt":
        # Through the operator's own control, with its audit event.
        await EntryHaltService(sessions, clock=FixedClock(now)).set_manual(
            reason="OPERATOR_REVIEW", actor="kai"
        )
    elif account:
        await set_account(sessions, **account)

    refused = await build_recheck(sessions, now + LATER, **overrides).open_recheck(rejected.id)

    assert refused.reason is expected
    assert await market_cases(sessions) == 1


async def test_observe_mode_opens_nothing(risk_db, now, trace):
    from src.core.models import TradingMode

    _, sessions = risk_db
    rejected = await rejected_with(
        sessions, now, trace, key="observe", codes=("LIQUIDITY_FDV_RATIO_LIMIT",)
    )
    refused = await build_recheck(
        sessions, now + LATER, trading_mode=TradingMode.OBSERVE
    ).open_recheck(rejected.id)
    assert refused.reason is RecheckRefusal.MODE_NOT_PAPER
    assert refused.detail == "OBSERVE"


async def test_a_missing_account_is_not_a_refusal(risk_db, now, trace):
    from src.data.tables import AccountRow

    _, sessions = risk_db
    rejected = await rejected_with(
        sessions, now, trace, key="account", codes=("LIQUIDITY_FDV_RATIO_LIMIT",)
    )
    async with sessions.begin() as session:
        await session.delete(await session.get(AccountRow, 1))

    with pytest.raises(RecheckUnavailable):
        await build_recheck(sessions, now + LATER).open_recheck(rejected.id)


# ------------------------------------------------------------ the whole chain


@pytest.mark.parametrize(
    ("holders", "code"),
    [
        ({"total_supply_raw": THIN_SUPPLY}, "LIQUIDITY_FDV_RATIO_LIMIT"),
        ({"total_supply_raw": HEAVY_SUPPLY}, "LIQUIDITY_FDV_RATIO_UNVERIFIED"),
        ({"total_supply_decimals": None}, "FDV_UNKNOWN"),
    ],
)
async def test_a_successor_is_approved_only_on_its_own_fresh_evidence(
    risk_db, now, trace, holders, code
):
    """The real SENTINEL refuses, the successor starts empty and proves itself."""
    _, sessions = risk_db
    rejected, verdict = await rejected_by_sentinel(sessions, now, trace, key="chain", **holders)
    assert verdict.reason_codes == (code,)
    before = await history(sessions, rejected.id)

    at = now + LATER
    opened = await build_recheck(sessions, at).open_recheck(rejected.id)
    assert opened.kind == "recheck_opened", getattr(opened, "reason", None)
    assert opened.reason_codes == (code,)

    service = build_service(sessions, at)
    successor = await service.cases.get_trade_case(opened.trade_case_id)
    # The workflow's own starting state, with nothing carried over.
    assert successor.status is TradeCaseStatus.EVIDENCE_PENDING
    assert successor.risk_input_digest is None
    assert successor.originating_discovery_reference == rejected.id
    assert successor.strategy_policy_id == rejected.strategy_policy_id
    assert successor.workflow_version == rejected.workflow_version
    # Only the discovery the workflow records for every case it opens; nothing
    # of the predecessor's evidence is carried over.
    own = await service.cases.evidence(successor.id)
    inherited = {item.evidence_id for item in await service.cases.evidence(rejected.id)}
    assert [item.evidence_type.value for item in own] == ["DISCOVERY_EVIDENCE"]
    assert {item.evidence_id for item in own}.isdisjoint(inherited)
    async with sessions() as session:
        bound = await session.scalar(
            select(TradeCaseRiskBindingRow).where(
                TradeCaseRiskBindingRow.trade_case_id == successor.id
            )
        )
    assert bound is None

    # Without evidence it cannot even be asked about.
    early = await service.request_risk_evaluation(successor.id, request_key="too-soon")
    assert early.kind == "risk_request_refused"

    ready = await evidence_for(service.cases, successor.id, at, onchain=fresh_onchain(at))
    assert ready.status is TradeCaseStatus.READY_FOR_RISK
    approved = await service.request_risk_evaluation(successor.id, request_key="successor-risk")
    assert approved.authorization is RiskAuthorization.APPROVED
    assert (await service.cases.get_trade_case(successor.id)).status is (
        TradeCaseStatus.RISK_APPROVED
    )

    # The rejected case is exactly as it was.
    assert await history(sessions, rejected.id) == before


async def test_a_successor_whose_ratio_is_still_wrong_is_rejected_again(risk_db, now, trace):
    _, sessions = risk_db
    rejected, _ = await rejected_by_sentinel(
        sessions, now, trace, key="still", total_supply_raw=THIN_SUPPLY
    )
    at = now + LATER
    opened = await build_recheck(sessions, at).open_recheck(rejected.id)
    service = build_service(sessions, at)
    await evidence_for(
        service.cases,
        opened.trade_case_id,
        at,
        onchain=onchain_with(at, total_supply_raw=THIN_SUPPLY),
    )

    again = await service.request_risk_evaluation(
        opened.trade_case_id, request_key="still-successor-risk"
    )

    assert again.outcome is RiskOutcome.REJECT
    assert again.reason_codes == ("LIQUIDITY_FDV_RATIO_LIMIT",)
    assert (await service.cases.get_trade_case(opened.trade_case_id)).status is (
        TradeCaseStatus.RISK_REJECTED
    )


async def test_intake_keeps_the_market_barred(risk_db, now, trace):
    _, sessions = risk_db
    rejected = await rejected_with(
        sessions, now, trace, key="intake", codes=("LIQUIDITY_FDV_RATIO_LIMIT",)
    )
    opened = await build_recheck(sessions, now + LATER).open_recheck(rejected.id)

    async with sessions() as session:
        # The predecessor still speaks for the market; the successor is live.
        assert await market_barring(session, PAIR_ID) is TradeCaseStatus.RISK_REJECTED
        assert await active_case_exists(session, PAIR_ID) is True
        event = await session.scalar(
            select(TradeCaseEventRow).where(
                TradeCaseEventRow.trade_case_id == opened.trade_case_id,
                TradeCaseEventRow.event_type == "LIQ_FDV_RECHECK_OPENED",
            )
        )
        successor = await session.get(TradeCaseRow, opened.trade_case_id)
    assert event.payload["predecessor_trade_case_id"] == str(rejected.id)
    assert event.payload["reason_codes"] == ["LIQUIDITY_FDV_RATIO_LIMIT"]
    assert successor.open_idempotency_key == recheck_key(rejected.id)

    refused = await build_recheck(sessions, now + LATER).open_recheck(opened.trade_case_id)
    assert refused.reason is RecheckRefusal.PREDECESSOR_NOT_RISK_REJECTED


# ------------------------------------------------------------ concurrency


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="row locks need PostgreSQL")
async def test_two_callers_on_one_predecessor_open_one_successor(risk_db, now, trace):
    _, sessions = risk_db
    rejected = await rejected_with(
        sessions, now, trace, key="race", codes=("LIQUIDITY_FDV_RATIO_LIMIT",)
    )

    results = await asyncio.gather(
        build_recheck(sessions, now + LATER).open_recheck(rejected.id),
        build_recheck(sessions, now + LATER).open_recheck(rejected.id),
    )

    assert {item.kind for item in results} == {"recheck_opened"}
    assert {item.trade_case_id for item in results} == {results[0].trade_case_id}
    assert sorted(item.replayed for item in results) == [False, True]
    assert await market_cases(sessions) == 2


def test_the_policy_refuses_an_unbounded_shape():
    from dataclasses import replace

    with pytest.raises(ValueError):
        replace(LIQ_FDV_RECHECK_V1, eligible_codes=frozenset())
    with pytest.raises(ValueError):
        replace(LIQ_FDV_RECHECK_V1, max_window=LIQ_FDV_RECHECK_V1.min_interval)
    with pytest.raises(ValueError):
        replace(LIQ_FDV_RECHECK_V1, max_per_market=0)
    assert "FDV_DECIMALS_MISMATCH" not in LIQ_FDV_RECHECK_V1.eligible_codes


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"enabled_chains": frozenset({"bsc"})}, RecheckRefusal.CHAIN_NOT_ENABLED),
        # The fixture case is a normal one, which early-only operation never works.
        ({"early_only": True}, RecheckRefusal.STRATEGY_NOT_RUN),
    ],
)
async def test_intakes_operational_bars_hold(risk_db, now, trace, overrides, expected):
    _, sessions = risk_db
    rejected = await rejected_with(
        sessions, now, trace, key="bars", codes=("LIQUIDITY_FDV_RATIO_LIMIT",)
    )

    refused = await build_recheck(sessions, now + LATER, **overrides).open_recheck(rejected.id)

    assert refused.reason is expected
    assert await market_cases(sessions) == 1


async def test_a_fixture_market_is_never_rechecked(risk_db, now, trace):
    _, sessions = risk_db
    rejected = await rejected_with(
        sessions, now, trace, key="fixture", codes=("LIQUIDITY_FDV_RATIO_LIMIT",)
    )
    async with sessions.begin() as session:
        row = await session.get(TradeCaseRow, rejected.id)
        row.market_payload = {**row.market_payload, "is_fixture": True}

    refused = await build_recheck(sessions, now + LATER).open_recheck(rejected.id)

    assert refused.reason is RecheckRefusal.FIXTURE_MARKET
    assert await market_cases(sessions) == 1


async def test_a_successor_keeps_its_predecessors_early_strategy(risk_db, now, trace):
    """An early market is re-checked as an early market, under the same policy."""
    from src.orchestration.strategy.early import PRE_VECTOR_EARLY_ENTRY_V1

    _, sessions = risk_db
    rejected = await rejected_with(
        sessions, now, trace, key="early", codes=("LIQUIDITY_FDV_RATIO_LIMIT",)
    )
    async with sessions.begin() as session:
        # Only the strategy label differs from the fixture case; the workflow
        # and evidence stay the fixture's.
        await session.execute(
            TradeCaseRow.__table__.update()
            .where(TradeCaseRow.id == rejected.id)
            .values(strategy_policy_id=PRE_VECTOR_EARLY_ENTRY_V1)
        )

    opened = await build_recheck(sessions, now + LATER, early_only=True).open_recheck(rejected.id)

    assert opened.kind == "recheck_opened", getattr(opened, "reason", None)
    successor = await TradeCaseService(sessions, clock=FixedClock(now + LATER)).get_trade_case(
        opened.trade_case_id
    )
    assert successor.strategy_policy_id == PRE_VECTOR_EARLY_ENTRY_V1
