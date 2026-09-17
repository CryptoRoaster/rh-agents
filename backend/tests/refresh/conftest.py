"""Fixtures for the evidence-refresh phase.

Everything runs through the production `build_stack`. What is supplied is the
outside edge only — the model, the chain read, the holder and origin indexers,
the social source, the market structure series and the quote source — and each
of those can be re-supplied at a later instant, which is the whole point here: a
source is only fresh again because somebody observed the world again.
"""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from src.core.clock import FixedClock
from src.data.tables import (
    TradeCaseEvidenceRow,
    TradeCaseRow,
    TradeCaseTaskRow,
    WorkerTaskAttemptRow,
)
from src.markets.recorder import MarketRecorder
from src.runner.composition import RunnerPorts
from tests.riskdata.conftest import CHAIN, NETWORK, QUOTE, recorded_snapshot
from tests.runner.conftest import (  # noqa: F401
    FRESH,
    risk_db,  # noqa: F401
    runner_settings,
)
from tests.runner.specialists import ScriptedSpecialists
from tests.runner.test_end_to_end import PAYMENT_POOL, SPOT

# PULSE re-checks on a ninety-second interval by workflow policy. The second
# pass happens after that, which is exactly when the first run's on-chain
# reading has aged past SENTINEL's thirty-second bound.
RECHECK = timedelta(seconds=95)

# Enough of a move to satisfy the setup VECTOR writes, so the trigger really is
# found on the second pass rather than being arranged.
SPOT_UP = Decimal("1.20")


async def record_market_at(sessions, instant, *, price=SPOT, label=""):
    """One recorded observation of the traded market, at that instant."""
    snapshot = recorded_snapshot(instant, age=FRESH, metadata_age=FRESH, price=price, label=label)
    await MarketRecorder(sessions, clock=FixedClock(instant)).record(snapshot)
    return snapshot


async def record_payment_at(sessions, instant, *, label="quote"):
    """The payment asset's own market, which ANCHOR needs to size a ladder."""
    snapshot = recorded_snapshot(
        instant,
        age=FRESH,
        metadata_age=FRESH,
        base_asset_id=f"{CHAIN}:{NETWORK}:{QUOTE}",
        quote_address="0x" + "dd" * 20,
        pair_id=f"{CHAIN}:{NETWORK}:contract_address:{PAYMENT_POOL}",
        label=label,
        price=Decimal("1"),
    )
    await MarketRecorder(sessions, clock=FixedClock(instant)).record(snapshot)
    return snapshot


def chain_sources(instant, *, holders=None):
    """ATLAS's two chain-side sources, as the world looked at that instant.

    Kept separate because they are what a refresh actually re-reads. Pinning
    them to an earlier instant while everything else moves on is how a test says
    "the chain has not been observed again", which is a different situation from
    "nobody looked".
    """
    from tests.atlas.conftest import (
        StubContracts,
        StubHolders,
        chain_snapshot,
        contract_facts,
        holder_rows,
        holder_source_result,
    )

    dispersed = holder_source_result(
        instant, rows=holder_rows(count=12, top_balance=8_000 * 10**18, step=200 * 10**18)
    )
    return {
        "onchain": StubContracts(chain_snapshot(instant), contract_facts()),
        "holders": StubHolders(dispersed if holders is None else holders),
    }


def ports_at(instant, model, **overrides):
    """Every external boundary, observed at that instant.

    Re-supplying these at a later instant is how a source becomes fresh again.
    Handing back the *same* observation would not, and a test that does exactly
    that is one of the proofs below.
    """
    from tests.anchor.conftest import source as quote_source
    from tests.atlas.conftest import StubOrigins, origin_facts
    from tests.signal.conftest import organic_set, source_for
    from tests.vector.conftest import StubHistory, history_for

    defaults: dict[str, object] = {
        "reasoning": model,
        **chain_sources(instant),
        "origins": StubOrigins(origin_facts()),
        "social": source_for(organic_set(instant)),
        "history": StubHistory(
            history_for(instant.replace(minute=0, second=0, microsecond=0), price=SPOT)
        ),
        "quotes": quote_source(instant, reference_price=SPOT),
    }
    return RunnerPorts(**{**defaults, **overrides})  # type: ignore[arg-type]


def all_specialists(**overrides):
    """Every role this runtime can claim, switched on deliberately."""
    defaults: dict[str, object] = {
        "paper_runner_max_candidates": 1,
        "orbit_worker_enabled": True,
        "atlas_worker_enabled": True,
        "signal_worker_enabled": True,
        "signal_social_provider": "neynar",
        "neynar_api_key": "unused-because-the-source-is-supplied",
        "vector_worker_enabled": True,
        "fuse_worker_enabled": True,
        "pulse_worker_enabled": True,
        "anchor_worker_enabled": True,
        "evm_runtime_enabled": True,
        "reasoning_provider": "anthropic",
    }
    return runner_settings(**{**defaults, **overrides})


def scripted():
    return ScriptedSpecialists()


async def first_pass(sessions, instant, model, **overrides):
    """The pass that assembles the case and leaves PULSE waiting.

    Everything the case is built from is observed at `instant`, which is the
    whole point: ninety-five seconds later those readings are what the risk
    engine would be asked to judge.
    """
    from tests.runner.conftest import run

    await record_market_at(sessions, instant)
    await record_payment_at(sessions, instant)
    settings = all_specialists(**overrides)
    return settings, await run(sessions, settings, instant, ports=ports_at(instant, model))


async def at_the_recheck(sessions, instant, *, price=SPOT_UP, label="moved"):
    """Move to the monitor's next due check, with the market observed again.

    `label` distinguishes one recorded observation from another: the recorder
    refuses two different readings under one event identity, which is the
    property that makes "observed again" mean something.
    """
    later = instant + RECHECK
    await record_market_at(sessions, later, price=price, label=label)
    await record_payment_at(sessions, later, label=f"quote-{label}")
    return later


async def traded_case(sessions):
    from tests.riskdata.conftest import PAIR_ID

    async with sessions() as session:
        return await session.scalar(select(TradeCaseRow).where(TradeCaseRow.market_key == PAIR_ID))


async def traded_case_id(sessions):
    """The case for the market under test.

    Everything below is scoped to it. Intake legitimately opens a case for the
    payment asset's own market too, and counting its evidence as if it belonged
    to the traded case would make a test read freshness off the wrong subject.
    """
    case = await traded_case(sessions)
    assert case is not None, "no trade case for the traded market"
    return case.id


async def evidence_rows(sessions, kind=None):
    case_id = await traded_case_id(sessions)
    async with sessions() as session:
        statement = select(TradeCaseEvidenceRow).where(
            TradeCaseEvidenceRow.trade_case_id == case_id
        )
        if kind is not None:
            statement = statement.where(TradeCaseEvidenceRow.evidence_type == kind.value)
        return (await session.scalars(statement)).all()


async def task_row(sessions, role, task_type):
    case_id = await traded_case_id(sessions)
    async with sessions() as session:
        return await session.scalar(
            select(TradeCaseTaskRow).where(
                TradeCaseTaskRow.trade_case_id == case_id,
                TradeCaseTaskRow.role == role.value,
                TradeCaseTaskRow.task_type == task_type,
            )
        )


async def attempts(sessions, role=None):
    case_id = await traded_case_id(sessions)
    async with sessions() as session:
        statement = select(WorkerTaskAttemptRow).where(
            WorkerTaskAttemptRow.trade_case_id == case_id
        )
        if role is not None:
            statement = statement.where(WorkerTaskAttemptRow.role == role.value)
        return (await session.scalars(statement)).all()
