"""Fixtures for the control-plane tests.

Most questions here are about what COMMANDER declines to do, so the fixtures
build whole cases at particular workflow stages rather than isolated objects.
"""

from decimal import Decimal
from uuid import uuid4

import pytest

from src.core.clock import FixedClock
from src.core.models import AgentRole
from src.orchestration.commander.context import CommanderContextReader
from src.orchestration.commander.intake import CommanderIntakeService
from src.orchestration.worker.service import WorkerRuntimeService
from src.orchestration.workflow.models import EvidenceType
from src.orchestration.workflow.service import TradeCaseService
from tests.fuse.conftest import onchain, onchain_envelope_kwargs, sentiment, trade_setup
from tests.worker.conftest import submission
from tests.worker.conftest import worker_db as worker_db  # noqa: F401


class RunningSystem:
    """A deployment whose stop source is configured and reports no stop.

    Supplied explicitly because the control plane fails closed without one: an
    unconfigured stop is unknown, and unknown is not permission. Tests that want
    "the system is running" have to say so, which is the same thing a real
    deployment has to do.
    """

    def __init__(self, paused: bool = False) -> None:
        self.paused = paused

    async def system_paused(self) -> bool:
        return self.paused

    async def locked_paused(self, session) -> bool:
        """The transactional read, standing in for the account-row lock.

        A stub cannot provide the ordering a real lock does — that is exactly
        what the PostgreSQL concurrency tests exercise against the real reader.
        What it can do is answer the same question at the same point in the
        transaction, so every other test runs through the real code path.
        """
        return self.paused


def build_stack(sessions, instant, *, kill_switch=False, pause=None, mode="PAPER"):
    clock = FixedClock(instant)
    cases = TradeCaseService(sessions, clock=clock)
    runtime = WorkerRuntimeService(sessions, cases, clock=clock)
    reader = CommanderContextReader(
        cases=cases,
        sessions=sessions,
        clock=clock,
        kill_switch=kill_switch,
        pause=pause if pause is not None else RunningSystem(),
        trading_mode=mode,
    )
    return runtime, reader


async def open_case(cases, now, trace, key):
    from tests.worker.conftest import open_case as _open

    return await _open(cases, now, trace, key)


async def record(cases, trade_case, now, role, evidence_type, payload, *, key, **kw):
    """Record one envelope, honouring the workflow's own rules for its type."""
    if evidence_type is EvidenceType.ONCHAIN:
        kw = {**onchain_envelope_kwargs(payload), **kw}
    return await cases.record_evidence(
        trade_case.id,
        submission(trade_case, now, role, evidence_type, payload, key=key, **kw),
    )


async def inject_pre_trigger_evidence(cases, trade_case, now, **overrides):
    """Write ATLAS, SIGNAL and VECTOR evidence directly; ORBIT arrives at case open.

    Named for what it does. No specialist worker runs here — those need
    reasoning providers and market data this suite does not reach — so these
    tests exercise the workflow and the control plane against evidence of the
    right shape, not the specialists that would produce it. The one worker
    actually executed anywhere in this suite is FUSE.
    """
    await record(
        cases,
        trade_case,
        now,
        AgentRole.ATLAS,
        EvidenceType.ONCHAIN,
        overrides.get("onchain", onchain()),
        key=f"cmd-atlas-{trade_case.id}",
    )
    await record(
        cases,
        trade_case,
        now,
        AgentRole.SIGNAL,
        EvidenceType.SENTIMENT,
        overrides.get("sentiment", sentiment()),
        key=f"cmd-signal-{trade_case.id}",
    )
    return await record(
        cases,
        trade_case,
        now,
        AgentRole.VECTOR,
        EvidenceType.TRADE_SETUP,
        overrides.get("trade_setup", trade_setup(now)),
        key=f"cmd-setup-{trade_case.id}",
    )


async def inject_trigger(cases, trade_case, now, setup_evidence):
    from tests.worker.conftest import trigger_payload

    return await record(
        cases,
        trade_case,
        now,
        AgentRole.PULSE,
        EvidenceType.TRIGGER,
        trigger_payload(setup_evidence.evidence_id),
        key=f"cmd-trigger-{trade_case.id}",
    )


async def context_for(runtime, reader, trade_case):
    return await reader.commander_context(trade_case.id, uuid4())


class StubMarkets:
    """Recorded candidates and snapshots. Never a provider."""

    def __init__(self, candidates=(), snapshots=None) -> None:
        self._candidates = tuple(candidates)
        self._snapshots = snapshots or {}

    async def candidates(self, *, include_fixtures=False, limit=50, offset=0):
        return tuple(item for item in self._candidates if include_fixtures or not item.is_fixture)[
            offset : offset + limit
        ]

    async def latest(self, identity: str, *, include_fixtures: bool = False):
        return self._snapshots.get(identity)


def intake_service(sessions, instant, *, candidates=(), snapshots=None, **overrides):
    clock = FixedClock(instant)
    overrides.setdefault("pause", RunningSystem())
    return CommanderIntakeService(
        cases=TradeCaseService(sessions, clock=clock),
        markets=StubMarkets(candidates, snapshots),
        sessions=sessions,
        clock=clock,
        **overrides,
    )


@pytest.fixture
async def commander_db():
    """A schema carrying accounting *and* workflow, as a real deployment does.

    The worker fixture applies only the workflow migrations, which is the right
    slice for everything that never touches the paper account. The transactional
    pause proofs do: they need the real `AccountPauseReader` taking the real row
    lock, because an injected boolean port cannot provide ordering and a test
    built on one would prove nothing about the race it claims to close.

    `paper_accounts` has been in the regular Alembic chain since 0001.
    """
    import importlib.util
    import os
    from pathlib import Path
    from uuid import uuid4

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("PostgreSQL required for transactional pause proofs")

    schema = "commander_test_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        versions = Path(__file__).parents[2] / "migrations/versions"
        modules = []
        for name in ("0001_foundation", "0005_trade_case_workflow", "0006_worker_runtime"):
            spec = importlib.util.spec_from_file_location(name, versions / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            modules.append(module)

        def migrate(sync_connection):
            with Operations.context(MigrationContext.configure(sync_connection)):
                for item in modules:
                    item.upgrade()

        async with engine.begin() as connection:
            await connection.run_sync(migrate)
        yield engine, async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def seed_account(sessions, *, paused: bool = False):
    """Ensure the single authoritative paper account exists and set its pause.

    Migration 0001 seeds the row, so this updates rather than inserts — the
    `single_paper_account` constraint means there is exactly one, which is the
    identity the pause reader addresses.
    """
    from sqlalchemy import select, update

    from src.data.tables import AccountRow

    async with sessions.begin() as session:
        existing = await session.scalar(select(AccountRow.id).where(AccountRow.id == 1))
        if existing is None:
            from datetime import date

            session.add(
                AccountRow(
                    id=1,
                    cash_usd=Decimal("10000"),
                    initial_cash_usd=Decimal("10000"),
                    fees_paid_usd=Decimal("0"),
                    loss_day=date(2026, 9, 9),
                    realized_loss_today_usd=Decimal("0"),
                    paused=paused,
                )
            )
        else:
            await session.execute(
                update(AccountRow).where(AccountRow.id == 1).values(paused=paused)
            )
