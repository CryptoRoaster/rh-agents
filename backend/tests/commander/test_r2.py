"""Regressions for hardening round two.

Four defects an independent review reproduced against 094cad3. Each is closed
here, and each proof insists on the thing that made the original fix
insufficient: a real lock rather than an injected boolean, a fixture built with
the *previous* model rather than the current one, a sum rather than a set, and
a decision made later from a context read earlier.
"""

import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from src.core.clock import FixedClock
from src.data.tables import AccountRow
from src.orchestration.commander.context import AccountPauseReader
from src.orchestration.commander.decision import decide
from src.orchestration.commander.intake import CommanderIntakeService, IntakeRefusal
from src.orchestration.commander.models import CommanderDisposition, CommanderReason
from src.orchestration.commander.policy import COMMANDER_CONTROL_V1
from src.orchestration.workflow.service import TradeCaseService
from tests.commander.conftest import StubMarkets, build_stack, seed_account
from tests.commander.test_intake import candidate_for


def real_intake(sessions, instant, pairs):
    """Intake wired to the real account-backed pause reader, not a stub."""
    candidates, snapshots = [], {}
    for candidate, snapshot in pairs:
        candidates.append(candidate)
        snapshots[candidate.pair_id] = snapshot
    clock = FixedClock(instant)
    return CommanderIntakeService(
        cases=TradeCaseService(sessions, clock=clock),
        markets=StubMarkets(candidates, snapshots),
        sessions=sessions,
        clock=clock,
        pause=AccountPauseReader(sessions=sessions),
    )


async def set_pause(sessions, value: bool) -> None:
    """Write the pause exactly as the paper service does: lock the row, then set."""
    async with sessions.begin() as session:
        await session.execute(select(AccountRow).where(AccountRow.id == 1).with_for_update())
        await session.execute(update(AccountRow).where(AccountRow.id == 1).values(paused=value))


# =================================================== R2-1: pause and open


async def test_a_pause_committed_before_the_open_wins(commander_db, now, trace):
    """Pause first, then open: the opening transaction observes the stop.

    The two contend for the same row lock the paper service takes, so the
    database orders them rather than timing doing it.
    """
    _, sessions = commander_db
    await seed_account(sessions)
    candidate, snapshot = candidate_for(now)

    await set_pause(sessions, True)
    outcome = await real_intake(sessions, now, [(candidate, snapshot)]).run_cycle()

    assert outcome.opened_count == 0
    # Refused at the top of the cycle, before any candidate is considered.
    assert outcome.refused == (("*", IntakeRefusal.SYSTEM_PAUSED),)
    service = real_intake(sessions, now, [(candidate, snapshot)])
    assert await service.cases.list_trade_cases() == ()


async def test_a_pause_committed_after_the_open_does_not_undo_it(commander_db, now, trace):
    """Open first, then pause: the case stands, and the next cycle is stopped.

    The documented order. A stop cannot retroactively cancel work that had
    already committed when it arrived, and nothing pretends otherwise.
    """
    _, sessions = commander_db
    await seed_account(sessions)
    candidate, snapshot = candidate_for(now)

    first = await real_intake(sessions, now, [(candidate, snapshot)]).run_cycle()
    assert first.opened_count == 1

    await set_pause(sessions, True)
    second = await real_intake(sessions, now, [(candidate, snapshot)]).run_cycle()
    assert second.opened_count == 0
    assert second.refused == (("*", IntakeRefusal.SYSTEM_PAUSED),)

    service = real_intake(sessions, now, [(candidate, snapshot)])
    assert len(await service.cases.list_trade_cases()) == 1


async def test_a_pause_racing_the_open_is_ordered_by_the_row_lock(commander_db, now, trace):
    """Both transactions in flight at once, resolved by the lock rather than luck.

    The opening transaction is held at the market read — after every check the
    previous implementation performed — and the pause writer starts while it
    waits. Whichever acquires the account row first decides the outcome, and
    both possible outcomes are consistent: either no case, or a case plus a
    pause that took effect after it.
    """
    engine, sessions = commander_db
    await seed_account(sessions)
    candidate, snapshot = candidate_for(now)

    reached_latest = asyncio.Event()
    release = asyncio.Event()
    service = real_intake(sessions, now, [(candidate, snapshot)])
    original = service.markets.latest

    async def held(identity, **kwargs):
        result = await original(identity, **kwargs)
        reached_latest.set()
        await release.wait()
        return result

    service.markets.latest = held

    async def pause_writer():
        await reached_latest.wait()
        writer = asyncio.create_task(set_pause(sessions, True))
        await asyncio.sleep(0.05)
        release.set()
        await writer

    outcome, _ = await asyncio.gather(service.run_cycle(), pause_writer())

    cases = await real_intake(sessions, now, []).cases.list_trade_cases()
    async with sessions() as session:
        paused = await session.scalar(select(AccountRow.paused).where(AccountRow.id == 1))
    assert paused is True

    # Exactly one of the two documented orders, and never both.
    if outcome.opened_count == 1:
        assert len(cases) == 1, "the open committed before the pause"
    else:
        assert cases == (), "the pause committed first, so nothing opened"
        assert (candidate.pair_id, IntakeRefusal.SYSTEM_PAUSED) in outcome.refused


async def test_an_injected_boolean_alone_is_not_the_guarantee(commander_db, now, trace):
    """The distinction the review insisted on, made explicit.

    The real reader takes the writer's own lock; a stub answering the same
    question cannot order anything. This asserts the production path actually
    locks, so a future refactor that drops the `FOR UPDATE` is caught here
    rather than in a race nobody reproduces.
    """
    import inspect

    source = inspect.getsource(AccountPauseReader.locked_paused)
    assert "with_for_update" in source
    assert "AccountRow.id == self.account_id" in source


# ================================= R2-2: historical payloads and replay


def previous_anchor_detail(now) -> dict:
    """An ANCHOR detail exactly as 9515b49 built and stored it.

    Written as the dictionary that model produced rather than by constructing
    the current one, because the point is to read something the current code
    cannot create. Parsing a payload the present model also emits would prove
    nothing about history.
    """
    return {
        "policy_version": "anchor-execution-v1",
        "capacity_semantics": "AT_LEAST",
        "reason_code": "CAPACITY_AT_LEAST_TESTED_CEILING",
        "largest_tested_acceptable_notional_usd": "50000",
        "reference_price": "1.00",
        "reference_price_basis": "USD_PER_BASE_UNIT",
        "reference_observed_at": now.isoformat(),
        "quote_asset_usd_price": "1.00",
        "quote_asset_usd_observed_at": now.isoformat(),
        "quote_asset_usd_provider": "geckoterminal",
        "payment_asset_id": "robinhood:mainnet:0xaa",
        "target_asset_id": "robinhood:mainnet:0xbb",
        "quote_provider": "kyberswap",
        "quote_requests": 5,
        "ladder": [
            {
                "notional_usd": "100",
                "amount_in_tokens": "100",
                "accepted": True,
                "amount_out": 99,
                "effective_price_usd": "1.0002",
            }
        ],
        "evaluated_at": now.isoformat(),
        "execution_digest": "a" * 64,
    }


def previous_synthesis_detail(now) -> dict:
    """A FUSE detail exactly as 9515b49 built and stored it."""
    return {
        "policy_version": "fuse-synthesis-v1",
        "disposition": "COHERENT",
        "sources": [
            {
                "role": "ATLAS",
                "evidence_type": "ONCHAIN_EVIDENCE",
                "evidence_id": str(uuid4()),
                "submission_fingerprint": "b" * 64,
                "status": "AVAILABLE",
                "acceptance": "ACCEPTED",
                "observed_at": now.isoformat(),
                "valid_until": (now + timedelta(minutes=30)).isoformat(),
                "required": True,
                "safety_critical": True,
            }
        ],
        "input_digest": "c" * 64,
        "synthesis_fingerprint": "d" * 64,
        "evaluated_at": now.isoformat(),
    }


def test_historical_details_are_readable_through_the_current_models(now):
    """Evidence is append-only, so rows written before the change still exist."""
    from src.orchestration.workflow.models import (
        ExecutionAssessmentDetail,
        SynthesisDetail,
    )

    anchor = ExecutionAssessmentDetail.model_validate(previous_anchor_detail(now))
    synthesis = SynthesisDetail.model_validate(previous_synthesis_detail(now))

    assert anchor.legacy_evaluated_at == now
    assert synthesis.legacy_evaluated_at == now
    assert anchor.largest_tested_acceptable_notional_usd == Decimal("50000")
    assert synthesis.disposition == "COHERENT"


def test_a_historical_payload_survives_a_read_and_write_round_trip(now):
    """Reading is not enough: nothing may quietly drop the value on rewrite.

    Storage is append-only, so a round trip through the model must not silently
    rewrite what was recorded.
    """
    from src.orchestration.workflow.models import ExecutionAssessmentDetail

    original = ExecutionAssessmentDetail.model_validate(previous_anchor_detail(now))
    round_tripped = ExecutionAssessmentDetail.model_validate(original.model_dump(mode="json"))
    assert round_tripped.legacy_evaluated_at == now
    assert round_tripped == original


def test_the_contract_still_refuses_genuinely_unknown_fields(now):
    """No blanket `extra="ignore"`: a typo must still fail, not vanish."""
    from src.orchestration.workflow.models import (
        ExecutionAssessmentDetail,
        SynthesisDetail,
    )

    for model, payload in (
        (ExecutionAssessmentDetail, previous_anchor_detail(now)),
        (SynthesisDetail, previous_synthesis_detail(now)),
    ):
        with pytest.raises(ValueError):
            model.model_validate({**payload, "capacity_sematics": "AT_LEAST"})


def test_a_historical_detail_reserialises_to_the_bytes_it_was_given(now):
    """Round-trip fidelity of the detail itself.

    This is deliberately *not* a replay proof, and an earlier version of it
    claimed to be one. Re-validating a model's own output and comparing the two
    puts the current model on both sides, so it passes whatever the model does
    to a historical key — which is exactly how a shim that renamed one looked
    correct here while breaking replay. The real proof lives in
    `test_r3.py`, against fixtures produced by running the predecessor commits
    and carrying the fingerprints that code actually computed.
    """
    from src.orchestration.workflow.models import ExecutionAssessmentDetail

    given = previous_anchor_detail(now)
    parsed = ExecutionAssessmentDetail.model_validate(given)
    dumped = json.loads(parsed.model_dump_json())
    # Same instant, emitted under the historical key rather than the field name.
    assert parsed.legacy_evaluated_at == now
    assert "evaluated_at" in dumped
    assert "legacy_evaluated_at" not in dumped


async def test_new_results_computed_at_different_times_still_replay(worker_db, now, trace):
    """New-versus-new: identical evidence, different computation time, one record."""
    from tests.commander.conftest import inject_pre_trigger_evidence, open_case
    from tests.commander.test_hardening import _synthesize

    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "r2-replay-new")
    await inject_pre_trigger_evidence(runtime.cases, trade_case, now)

    first = await _synthesize(runtime, trade_case, now, trace)
    second = await _synthesize(runtime, trade_case, now + timedelta(seconds=3), trace)
    assert first.idempotency_key == second.idempotency_key
    assert first.fingerprint() == second.fingerprint()
    assert first.payload.synthesis.legacy_evaluated_at is None


async def test_a_real_content_change_is_still_a_different_result(worker_db, now, trace):
    """The control: compatibility must not make genuine differences invisible."""
    from src.orchestration.workflow.engine import active_evidence
    from src.orchestration.workflow.models import EvidenceType
    from tests.commander.conftest import inject_pre_trigger_evidence, open_case, record
    from tests.commander.test_hardening import _synthesize
    from tests.fuse.conftest import sentiment

    _, sessions = worker_db
    runtime, _ = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "r2-replay-changed")
    await inject_pre_trigger_evidence(runtime.cases, trade_case, now)
    first = await _synthesize(runtime, trade_case, now, trace)

    later = now + timedelta(minutes=1)
    late_runtime, _ = build_stack(sessions, later)
    current = active_evidence(await runtime.cases.evidence(trade_case.id))
    await record(
        late_runtime.cases,
        trade_case,
        later,
        __import__("src.core.models", fromlist=["x"]).AgentRole.SIGNAL,
        EvidenceType.SENTIMENT,
        sentiment(assessment="NEGATIVE", data_quality="POOR"),
        key="r2-replay-signal-2",
        supersedes_id=current[EvidenceType.SENTIMENT].evidence_id,
    )
    second = await _synthesize(late_runtime, trade_case, later, trace)
    assert first.idempotency_key != second.idempotency_key
    assert first.fingerprint() != second.fingerprint()


# ================================================ R2-3: created versus replay


async def test_the_loser_of_an_open_race_reports_a_replay_not_an_opening(commander_db, now, trace):
    """The sum, not the set: two workers must not claim one case between them.

    One worker is held at the market read while the other opens and commits.
    Counting the case the first then receives as newly opened made the pair
    report two openings for one case, and spend two units of a budget that
    exists to bound *new* cases.
    """
    _, sessions = commander_db
    await seed_account(sessions)
    candidate, snapshot = candidate_for(now)

    reached = asyncio.Event()
    release = asyncio.Event()
    slow = real_intake(sessions, now, [(candidate, snapshot)])
    original = slow.markets.latest

    async def held(identity, **kwargs):
        result = await original(identity, **kwargs)
        reached.set()
        await release.wait()
        return result

    slow.markets.latest = held

    async def second():
        await reached.wait()
        outcome = await real_intake(sessions, now, [(candidate, snapshot)]).run_cycle()
        release.set()
        return outcome

    first_outcome, second_outcome = await asyncio.gather(slow.run_cycle(), second())

    cases = await real_intake(sessions, now, []).cases.list_trade_cases()
    assert len(cases) == 1
    assert first_outcome.opened_count + second_outcome.opened_count == 1
    loser = first_outcome if first_outcome.opened_count == 0 else second_outcome
    assert (candidate.pair_id, IntakeRefusal.ALREADY_OPENED) in loser.refused


async def test_two_observations_of_one_market_count_one_opening(commander_db, now, trace):
    """Different candidate identities, one market, one opening between them."""
    _, sessions = commander_db
    await seed_account(sessions)
    pair = f"robinhood:mainnet:contract_address:0x{41:040x}"
    early = candidate_for(now, seconds_ago=40, pair=pair)
    late = candidate_for(now, seconds_ago=10, pair=pair)
    assert early[0].id != late[0].id

    first = await real_intake(sessions, now, [early]).run_cycle()
    second = await real_intake(sessions, now, [late]).run_cycle()

    cases = await real_intake(sessions, now, []).cases.list_trade_cases()
    assert len(cases) == 1
    assert first.opened_count + second.opened_count == 1


async def test_a_successor_generation_counts_exactly_one_opening(commander_db, now, trace):
    """The generation boundary counts like any other opening: once."""
    _, sessions = commander_db
    await seed_account(sessions)
    candidate, snapshot = candidate_for(now)

    service = real_intake(sessions, now, [(candidate, snapshot)])
    first = await service.run_cycle()
    assert first.opened_count == 1
    await service.cases.cancel_trade_case(first.opened[0].id, reason_code="OPERATOR_CANCELLED")

    left = await real_intake(sessions, now, [(candidate, snapshot)]).run_cycle()
    right = await real_intake(sessions, now, [(candidate, snapshot)]).run_cycle()

    cases = await real_intake(sessions, now, []).cases.list_trade_cases()
    assert len(cases) == 2, "one predecessor and one successor"
    assert left.opened_count + right.opened_count == 1


async def test_the_cycle_budget_counts_only_new_cases(commander_db, now, trace):
    """A replay must not consume a slot meant for opening something new."""
    _, sessions = commander_db
    await seed_account(sessions)
    pairs = [
        candidate_for(now, pair=f"robinhood:mainnet:contract_address:0x{index:040x}")
        for index in range(51, 54)
    ]
    # Open the first market, so the next cycle meets one replay and two new ones.
    await real_intake(sessions, now, [pairs[0]]).run_cycle()
    outcome = await real_intake(sessions, now, pairs).run_cycle()

    assert outcome.opened_count == 2
    refusals = {reason for _, reason in outcome.refused}
    assert IntakeRefusal.ACTIVE_CASE_EXISTS in refusals
    cases = await real_intake(sessions, now, []).cases.list_trade_cases()
    assert len(cases) == 3


# ============================================= R2-4: context validity at decision


async def test_a_context_read_earlier_cannot_decide_later(worker_db, now, trace):
    """`now` decides whether a reading may still be used at all.

    The context freezes every temporal fact at read time, so a decision made
    later from the same view would repeat a verdict about a moment that has
    passed. Tested at the boundary with the *same* previously-read context.
    """
    from tests.commander.test_hardening import authorized_case

    _, sessions = worker_db
    ttl = timedelta(minutes=2)
    trade_case = await authorized_case(sessions, now, trace, "r2-4-boundary", ttl=ttl)
    _, reader = build_stack(sessions, now)
    context = await reader.commander_context(trade_case.id, uuid4())

    assert context.valid_until is not None
    boundary = context.valid_until

    before = decide(context, boundary - timedelta(microseconds=1), COMMANDER_CONTROL_V1)
    at = decide(context, boundary, COMMANDER_CONTROL_V1)
    after = decide(context, boundary + timedelta(microseconds=1), COMMANDER_CONTROL_V1)

    assert before.disposition == CommanderDisposition.RISK_CURRENT
    assert at.disposition == CommanderDisposition.STALE_CONTEXT
    assert at.reason_code == CommanderReason.CONTEXT_STALE
    assert after.disposition == CommanderDisposition.STALE_CONTEXT


async def test_a_fresh_context_at_the_later_time_decides_normally(worker_db, now, trace):
    """Refusing a stale reading demands a new one; it does not halt the case.

    Distinct from the test above: the same instant, a context built *then*,
    and an ordinary conclusion about an authorization that has since lapsed.
    """
    from tests.commander.test_hardening import authorized_case

    _, sessions = worker_db
    ttl = timedelta(minutes=2)
    trade_case = await authorized_case(sessions, now, trace, "r2-4-fresh", ttl=ttl)

    later = now + ttl + timedelta(minutes=5)
    _, reader = build_stack(sessions, later)
    fresh = await reader.commander_context(trade_case.id, uuid4())
    decision = decide(fresh, later, COMMANDER_CONTROL_V1)

    assert fresh.risk is not None
    assert fresh.risk.expired is True
    assert decision.disposition == CommanderDisposition.BLOCKED_ON_MISSING_CAPABILITY
    assert decision.reason_code == CommanderReason.AUTONOMOUS_SIZING_INPUT_MISSING


async def test_a_setup_expiring_after_the_read_also_ages_the_context(worker_db, now, trace):
    """The same boundary covers setup, case and evidence horizons, not only risk.

    The setup's own expiry can arrive before its envelope goes stale, so a view
    that ignored it would keep answering about geometry that had lapsed.
    """
    from tests.commander.conftest import inject_pre_trigger_evidence, open_case
    from tests.fuse.conftest import trade_setup

    _, sessions = worker_db
    runtime, reader = build_stack(sessions, now)
    trade_case = await open_case(runtime.cases, now, trace, "r2-4-setup")
    await inject_pre_trigger_evidence(
        runtime.cases,
        trade_case,
        now,
        trade_setup=trade_setup(now, expires_in=timedelta(minutes=3)),
    )

    context = await reader.commander_context(trade_case.id, uuid4())
    assert context.valid_until == now + timedelta(minutes=3)

    inside = decide(context, now + timedelta(minutes=2), COMMANDER_CONTROL_V1)
    outside = decide(context, now + timedelta(minutes=4), COMMANDER_CONTROL_V1)
    assert inside.disposition == CommanderDisposition.AWAIT_TRIGGER
    assert outside.disposition == CommanderDisposition.STALE_CONTEXT
