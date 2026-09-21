"""A price the ledger envelope cannot express must not crash the monitor.

The first real PAPER smoke run recorded one BNB Smart Chain pool and then wrote
`PULSE / FAILED_RETRYABLE / HANDLER_ERROR / INTERNAL` into the task history. The
market layer accepts any decimal a provider reports; PULSE compares prices in
the envelope the ledger stores them in — `Numeric(38, 18)` — and the recorded
price had nineteen decimal places. Building the observation raised a validation
error, which is not a `PulseContextUnavailable`, so it escaped the handler and
was classified by the runtime as an unknown handler bug.

The numbers and identities below are the ones from that run: a real pool, a real
price, and the instants it actually happened at. Everything between the recorded
observation and the durable attempt row is production code — the real stack, the
real worker runtime, real claims and leases, the real context reader and the real
handler. No provider is contacted.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select

from src.core.clock import FixedClock
from src.data.tables import (
    ExecutionRow,
    PositionRow,
    TradeCaseRiskRequestRow,
    TradeCaseRow,
    TradeCaseTaskRow,
    WorkerTaskAttemptRow,
)
from src.markets.models import (
    AssetIdentity,
    Availability,
    LiquiditySnapshot,
    MarketPair,
    MarketSnapshot,
    PoolLocator,
    PoolLocatorKind,
    PriceSnapshot,
    VolumeSnapshot,
)
from src.markets.recorder import MarketRecorder
from src.runner.models import ExitCode, RunStop
from tests.runner.conftest import read_account, run, runner_settings

# The instant the smoke run happened, and the market it observed. Kept exact:
# a price that fits the envelope proves nothing about the one that did not.
RAN_AT = datetime(2026, 9, 21, 4, 21, 58, 600000, tzinfo=UTC)
OBSERVED_AT = datetime(2026, 9, 21, 4, 21, 58, 532139, tzinfo=UTC)
CHAIN = "bsc"
NETWORK = "mainnet"
VENUE = "uniswap-v4-bsc"
POOL = "0x3c7dae8827a2abf82e8befa046aedd5506e5ac0aaa3faa9e47a70b9e87c6744b"
BASE = "0xc07244f3fb0d8e2bc9f8a0550698ca6d1c1f4ee2"
QUOTE = "0x55d398326f99059ff775485246999027b3197955"
PAIR_ID = f"{CHAIN}:{NETWORK}:bytes32_pool_id:{VENUE}:{POOL}"
# Nineteen decimal places. The market contract allows it; the ledger envelope
# PULSE compares in does not.
UNREPRESENTABLE = Decimal("0.0000469578606441642")
# The same market with a price that fits, for the control.
REPRESENTABLE = Decimal("0.000046957860644164")


def stable(label: str):
    return uuid5(NAMESPACE_URL, f"rh-agents:pulse-precision:{label}")


def observation(price: Decimal, *, observed_at=OBSERVED_AT, label: str = "") -> MarketSnapshot:
    """The recorded observation, built through the production market contracts."""
    tag = f"{label}:" if label else ""
    base_asset = f"{CHAIN}:{NETWORK}:{BASE}"
    meta = dict(
        observed_at=observed_at,
        provider="geckoterminal",
        chain=CHAIN,
        network=NETWORK,
        correlation_id=stable(f"{tag}trace"),
        is_fixture=False,
    )
    locator = PoolLocator(kind=PoolLocatorKind.BYTES32_POOL_ID, value=POOL, venue=VENUE)
    pair = MarketPair(
        **{**meta, "asset_id": base_asset},
        id=stable(f"{tag}pair"),
        pair_id=PAIR_ID,
        pool_locator=locator,
        base=AssetIdentity(
            **{**meta, "asset_id": base_asset},
            id=stable(f"{tag}base"),
            symbol="TKN",
            decimals=18,
        ),
        quote=AssetIdentity(
            **{**meta, "asset_id": f"{CHAIN}:{NETWORK}:{QUOTE}"},
            id=stable(f"{tag}quote"),
            symbol="USDT",
            decimals=18,
        ),
        venue=VENUE,
    )
    return MarketSnapshot(
        **{**meta, "asset_id": base_asset},
        id=stable(f"{tag}snapshot"),
        schema_version=2,
        pair=pair,
        price=PriceSnapshot(
            **{**meta, "asset_id": base_asset},
            id=stable(f"{tag}price"),
            status=Availability.AVAILABLE,
            value_usd=price,
        ),
        liquidity=LiquiditySnapshot(
            **{**meta, "asset_id": base_asset},
            id=stable(f"{tag}liquidity"),
            status=Availability.AVAILABLE,
            value_usd=Decimal("1.9372"),
        ),
        volume=VolumeSnapshot(
            **{**meta, "asset_id": base_asset},
            id=stable(f"{tag}volume"),
            status=Availability.AVAILABLE,
            value_usd=Decimal("13.8131921182"),
            window_seconds=86400,
        ),
    )


def smoke_settings(**overrides):
    """Exactly the roles and budgets the smoke run was given."""
    defaults: dict[str, object] = {
        "market_provider": "geckoterminal",
        "market_chains": "bsc",
        "paper_runner_max_candidates": 1,
        "paper_runner_max_new_cases": 1,
        "paper_runner_max_cases": 1,
        "paper_runner_max_steps": 8,
        "paper_runner_max_seconds": 120,
        "paper_runner_step_timeout_seconds": 30,
        "pulse_worker_enabled": True,
        "fuse_worker_enabled": True,
    }
    return runner_settings(**{**defaults, **overrides})


async def record(sessions, snapshot: MarketSnapshot) -> None:
    await MarketRecorder(sessions, clock=FixedClock(RAN_AT)).record(snapshot)


async def pulse_attempts(sessions):
    async with sessions() as session:
        return (
            await session.scalars(
                select(WorkerTaskAttemptRow)
                .where(WorkerTaskAttemptRow.role == "PULSE")
                .order_by(WorkerTaskAttemptRow.started_at)
            )
        ).all()


async def pulse_task(sessions):
    async with sessions() as session:
        return await session.scalar(
            select(TradeCaseTaskRow).where(TradeCaseTaskRow.role == "PULSE")
        )


async def counted(sessions, table):
    async with sessions() as session:
        return await session.scalar(select(func.count()).select_from(table))


# --------------------------------------------------------------- the reproduction


async def test_an_unrepresentable_price_leaves_the_monitor_waiting(risk_db, now, trace):
    """The smoke run's own market and instant, through the whole worker path.

    Reproduction against `28ec7142fe4e8101c4f1c47ce241cc0aabf9e87e`: this ended
    with `FAILED_RETRYABLE / HANDLER_ERROR / INTERNAL` on the PULSE attempt and
    the task rescheduled onto the *failure* backoff, spending one of the three
    attempts a real fault is entitled to.

    There is no setup to watch — VECTOR is not enabled — so the contract's answer
    is ordinary patience, and it must be reached whatever the price looks like.
    """
    _, sessions = risk_db
    await record(sessions, observation(UNREPRESENTABLE))

    summary = await run(sessions, smoke_settings(), RAN_AT)

    assert summary.exit_code is ExitCode.COMPLETED, summary
    assert summary.stop is RunStop.NOTHING_LEFT_TO_DO
    assert summary.cases_opened == 1, summary
    assert summary.errors == ()

    attempts = await pulse_attempts(sessions)
    assert [item.outcome for item in attempts] == ["WAITING"], [
        (item.outcome, item.reason_code, item.failure_category) for item in attempts
    ]
    assert attempts[0].reason_code == "NO_CURRENT_SETUP"
    assert attempts[0].failure_category is None, "waiting is not a failure category"

    task = await pulse_task(sessions)
    assert task.status == "PENDING"
    assert task.failure_category is None
    assert task.next_eligible_at is not None, "a monitor that waits is rescheduled"
    assert task.lease_id is None

    # And nothing downstream of the monitor happened.
    assert summary.risk_requests == 0 and summary.fills == 0
    assert await counted(sessions, TradeCaseRiskRequestRow) == 0
    assert await counted(sessions, ExecutionRow) == 0
    assert await counted(sessions, PositionRow) == 0
    account = await read_account(sessions)
    assert account.cash_usd == account.initial_cash_usd
    assert account.fees_paid_usd == 0 and account.paused is False


async def test_waiting_does_not_spend_the_failure_budget(risk_db, now, trace):
    """Patience has its own allowance, and the failure allowance stays whole."""
    _, sessions = risk_db
    await record(sessions, observation(UNREPRESENTABLE))

    await run(sessions, smoke_settings(), RAN_AT)

    attempts = await pulse_attempts(sessions)
    assert [item.failure_category for item in attempts] == [None]
    task = await pulse_task(sessions)
    # The monitor's own counter moved; no failure was recorded against it.
    assert task.attempt == 2
    assert task.failure_category is None


async def test_a_representable_price_behaves_identically(risk_db, now, trace):
    """The control: eighteen decimal places, same market, same answer.

    Present so the reproduction cannot be satisfied by a change that merely
    makes every price produce the same outcome.
    """
    _, sessions = risk_db
    await record(sessions, observation(REPRESENTABLE, label="fits"))

    summary = await run(sessions, smoke_settings(), RAN_AT)

    assert summary.exit_code is ExitCode.COMPLETED, summary
    attempts = await pulse_attempts(sessions)
    assert [(item.outcome, item.reason_code) for item in attempts] == [
        ("WAITING", "NO_CURRENT_SETUP")
    ]


async def test_one_pass_does_not_claim_the_same_waiting_task_twice(risk_db, now, trace):
    """A rescheduled monitor is not due again inside the pass that rescheduled it."""
    _, sessions = risk_db
    await record(sessions, observation(UNREPRESENTABLE))

    await run(sessions, smoke_settings(), RAN_AT)

    assert len(await pulse_attempts(sessions)) == 1
    task = await pulse_task(sessions)
    assert task.next_eligible_at > RAN_AT.replace(tzinfo=task.next_eligible_at.tzinfo)


async def test_an_unrepresentable_price_is_not_recorded_as_a_comparison(risk_db, now, trace):
    """The price is dropped as uncomparable, never rounded into range.

    Rounding would change what the market said in the one place where a
    comparison decides whether an order is armed.
    """
    from src.agents.pulse.context import price_observation
    from src.orchestration.workflow.service import TradeCaseService

    _, sessions = risk_db
    await record(sessions, observation(UNREPRESENTABLE))
    await run(sessions, smoke_settings(), RAN_AT)

    async with sessions() as session:
        case_id = await session.scalar(select(TradeCaseRow.id))
    trade_case = await TradeCaseService(sessions).get_trade_case(case_id)

    assert price_observation(observation(UNREPRESENTABLE), trade_case) is None
    fitting = price_observation(observation(REPRESENTABLE, label="fits"), trade_case)
    assert fitting is not None and fitting.price == REPRESENTABLE


async def test_a_real_handler_fault_is_still_reported_as_a_failure(risk_db, now, trace):
    """The classification that was wrong here must still work where it belongs."""
    from src.core.clock import FixedClock
    from src.runner.composition import RunnerPorts, build_stack
    from src.runner.service import BoundedPaperRun

    _, sessions = risk_db
    await record(sessions, observation(REPRESENTABLE, label="fits"))
    stack = build_stack(smoke_settings(), sessions, ports=RunnerPorts(), clock=FixedClock(RAN_AT))
    monitor = next(item for item in stack.runners if item.handler.role.value == "PULSE")

    async def breaks(*arguments, **keywords):
        raise RuntimeError("a genuine handler bug")

    object.__setattr__(monitor.handler, "handle", breaks)

    await BoundedPaperRun(stack).execute()

    attempts = await pulse_attempts(sessions)
    assert [(item.outcome, item.reason_code, item.failure_category) for item in attempts] == [
        ("FAILED_RETRYABLE", "HANDLER_ERROR", "INTERNAL")
    ]


def test_a_reached_trigger_still_produces_evidence(now):
    """A monitor that finds its condition still answers TRIGGERED.

    The existing PULSE fixtures, so the control uses the same evaluator the
    monitor uses rather than a second notion of what a crossing is.
    """
    from src.agents.pulse.handler import evaluate
    from src.agents.pulse.models import TriggerOutcome
    from src.agents.pulse.policy import PULSE_TRIGGER_V1
    from tests.pulse.conftest import observed, task_input

    reached = task_input(now, observation=observed(now, price=Decimal("1.50")))
    evaluation = evaluate(reached, reached.evaluated_at, PULSE_TRIGGER_V1)

    assert evaluation.outcome is TriggerOutcome.TRIGGERED, evaluation.reason_code


def test_an_unreached_trigger_keeps_waiting(now):
    """And one that has not been reached is still ordinary patience."""
    from src.agents.pulse.handler import evaluate
    from src.agents.pulse.models import TriggerOutcome
    from src.agents.pulse.policy import PULSE_TRIGGER_V1
    from tests.pulse.conftest import observed, task_input

    below = task_input(now, observation=observed(now, price=Decimal("0.90")))
    evaluation = evaluate(below, below.evaluated_at, PULSE_TRIGGER_V1)

    assert evaluation.outcome is TriggerOutcome.NOT_TRIGGERED


async def test_a_live_setup_with_an_unrepresentable_price_still_waits(risk_db, now, trace):
    """With something to watch, the answer is the contract's existing one.

    A price that cannot be compared leaves the monitor with nothing to compare,
    which the evaluator already answers as patience — `PRICE_UNAVAILABLE` — and
    not as a fault. The fix therefore changes which outcome is reached, never
    what any outcome means.
    """
    from src.agents.pulse.evaluator import evaluate
    from src.agents.pulse.models import WAITING_OUTCOMES, PulseReasonCode, TriggerOutcome
    from src.agents.pulse.policy import PULSE_TRIGGER_V1
    from tests.pulse.conftest import task_input

    watching = task_input(now, observation=None, latest=None)

    evaluation = evaluate(watching, watching.evaluated_at, PULSE_TRIGGER_V1)

    assert evaluation.outcome is TriggerOutcome.OBSERVATION_STALE
    assert evaluation.reason_code is PulseReasonCode.PRICE_UNAVAILABLE
    assert evaluation.outcome in WAITING_OUTCOMES


async def test_a_wiring_fault_in_the_observation_still_raises(risk_db, now, trace):
    """Only the envelope refusal is read as "no price".

    A market answering about something the case is not about is a wiring fault,
    and it must keep reaching the runtime as one rather than turning into a
    monitor that quietly sees nothing.
    """
    import pytest
    from pydantic import ValidationError

    from src.agents.pulse.context import price_observation
    from src.orchestration.workflow.service import TradeCaseService

    _, sessions = risk_db
    await record(sessions, observation(REPRESENTABLE, label="fits"))
    await run(sessions, smoke_settings(), RAN_AT)
    async with sessions() as session:
        case_id = await session.scalar(select(TradeCaseRow.id))
    trade_case = await TradeCaseService(sessions).get_trade_case(case_id)

    # A base asset identifier no contract would have produced, introduced the
    # only way it could reach here: by bypassing validation.
    broken = trade_case.model_copy(
        update={"market": trade_case.market.model_copy(update={"base_asset_id": "x" * 400})}
    )

    with pytest.raises(ValidationError):
        price_observation(observation(REPRESENTABLE, label="fits"), broken)
