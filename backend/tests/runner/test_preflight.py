"""What `--preflight` establishes, and everything it refuses to claim.

The production check path throughout: the real `Settings`, the real
`build_stack`, the real `RunnerStack.misconfigured`, the real `refuse()`, the
real schema and pause reads. Nothing here re-implements a check in order to
assert it.
"""

import asyncio
import json

import pytest
from sqlalchemy import func, select, text

from src.core.clock import FixedClock
from src.core.config import Settings
from src.data.schema import expected_revision
from src.data.tables import (
    AccountRow,
    ExecutionRow,
    MarketObservationRow,
    PositionRow,
    TradeCaseRow,
    WorkerInstanceRow,
    WorkerTaskAttemptRow,
)
from src.runner.composition import RunnerPorts, build_stack
from src.runner.main import main, render
from src.runner.models import ExitCode
from src.runner.preflight import CheckStatus, Preflight
from tests.runner.conftest import runner_settings

TABLES = (
    TradeCaseRow,
    WorkerInstanceRow,
    WorkerTaskAttemptRow,
    ExecutionRow,
    PositionRow,
    MarketObservationRow,
)


def preflight_settings(**overrides) -> Settings:
    """A configuration a run would be permitted to start from."""
    return runner_settings(**overrides)


async def check(sessions, settings, now, *, ports=None):
    """The production check, over the fixture's own database.

    `ports=None` composes from settings exactly as a deployment would, which is
    the path that matters for most of these: what a configuration can build is
    the question being asked.
    """
    stack = build_stack(settings, sessions, ports=ports, clock=FixedClock(now))
    return await Preflight(settings, sessions, clock=FixedClock(now)).run(stack)


def named(reading, name):
    return next(item for item in reading.checks if item.name == name)


def statuses(reading):
    return {item.name: item.status for item in reading.checks}


async def migrate_marker(sessions, revision=None):
    """Record a schema revision the way Alembic records one.

    The workflow fixtures apply migration modules directly, which leaves no
    version marker behind — so a test that wants the "database and code agree"
    case has to state it, and one that wants the opposite simply does not.
    """
    async with sessions.begin() as session:
        await session.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        await session.execute(text("DELETE FROM alembic_version"))
        await session.execute(
            text("INSERT INTO alembic_version VALUES (:v)"),
            {"v": revision if revision is not None else expected_revision()},
        )


async def counts(sessions):
    found = {}
    async with sessions() as session:
        for table in TABLES:
            found[table.__name__] = await session.scalar(select(func.count()).select_from(table))
        account = await session.scalar(select(AccountRow).where(AccountRow.id == 1))
        found["account"] = (
            account.cash_usd,
            account.fees_paid_usd,
            account.realized_loss_today_usd,
            account.paused,
        )
    return found


# ------------------------------------------------------------- a valid configuration


async def test_a_complete_configuration_is_reported_ready(risk_db, now):
    """Every locally answerable question answered, and nothing else claimed."""
    _, sessions = risk_db
    await migrate_marker(sessions)

    reading = await check(sessions, preflight_settings(pulse_worker_enabled=True), now)

    assert reading.ready is True, reading
    assert reading.blocked == ()
    assert reading.errors == ()
    assert reading.exit_code is ExitCode.COMPLETED
    found = statuses(reading)
    assert found["RUN_PERMITTED"] == CheckStatus.SATISFIED.value
    assert found["DATABASE_SCHEMA"] == CheckStatus.SATISFIED.value
    assert found["ACCOUNT_PAUSE"] == CheckStatus.SATISFIED.value
    assert found["ROLE_PULSE"] == CheckStatus.SATISFIED.value
    assert named(reading, "DATABASE_SCHEMA").note.endswith(f"revision {expected_revision()}.")
    # And the things it cannot know are named rather than assumed.
    assert found["CREDENTIAL_VALIDITY"] == CheckStatus.NOT_CHECKED.value
    assert named(reading, "CREDENTIAL_VALIDITY").reason == "REQUIRES_EXTERNAL_CALL"


async def test_readiness_says_nothing_about_reachability(risk_db, now):
    """A selected provider is selected. It is never reported as reachable."""
    _, sessions = risk_db
    await migrate_marker(sessions)
    settings = preflight_settings(
        market_provider="geckoterminal",
        market_chains="robinhood",
        reasoning_provider="anthropic",
        execution_quote_provider="kyberswap",
        evm_runtime_enabled=True,
        rh_chain_enabled=True,
        rh_rpc_http_url="https://rpc.invalid/rh",
        rh_rpc_ws_url="wss://rpc.invalid/rh",
    )

    reading = await check(sessions, settings, now)

    external = {
        item.name for item in reading.checks if item.status == CheckStatus.NOT_CHECKED.value
    }
    assert {
        "MARKET_PROVIDER_REACHABLE",
        "MODEL_PROVIDER_REACHABLE",
        "CHAIN_RPC_REACHABLE",
        "QUOTE_PROVIDER_REACHABLE",
        "CREDENTIAL_VALIDITY",
    } <= external, external
    # None of those blocks readiness: not knowing is not a fault.
    assert reading.ready is True, reading


# --------------------------------------------------------- incomplete configurations


async def test_a_run_that_is_not_permitted_is_reported_as_blocked(risk_db, now):
    """`refuse()` is the gate, and the preflight asks it rather than copying it."""
    _, sessions = risk_db
    await migrate_marker(sessions)
    settings = preflight_settings(paper_runner_enabled=False, trading_mode="OBSERVE")

    reading = await check(sessions, settings, now)

    permitted = named(reading, "RUN_PERMITTED")
    assert permitted.status == CheckStatus.BLOCKED.value
    assert permitted.reason == "PAPER_RUNNER_NOT_ENABLED"
    assert reading.ready is False
    assert reading.exit_code is ExitCode.CONFIGURATION_REFUSED
    assert reading.errors == (), "a missing setting is not a technical failure"


async def test_a_kill_switch_is_reported_by_the_same_gate(risk_db, now):
    _, sessions = risk_db
    await migrate_marker(sessions)

    reading = await check(sessions, preflight_settings(commander_kill_switch=True), now)

    assert named(reading, "RUN_PERMITTED").reason == "KILL_SWITCH_ENGAGED"
    assert reading.exit_code is ExitCode.CONFIGURATION_REFUSED


async def test_an_enabled_role_that_cannot_be_composed_blocks(risk_db, now):
    """The same set the executing CLI refuses a run over, reported per role."""
    _, sessions = risk_db
    await migrate_marker(sessions)
    # ORBIT is on, and the deterministic provider is deliberately not something
    # a configuration can build.
    settings = preflight_settings(orbit_worker_enabled=True, reasoning_provider="fake")

    reading = await check(sessions, settings, now)

    orbit = named(reading, "ROLE_ORBIT")
    assert orbit.status == CheckStatus.BLOCKED.value
    assert orbit.reason == "REASONING_PROVIDER_NOT_COMPOSABLE"
    assert reading.ready is False
    assert reading.exit_code is ExitCode.CONFIGURATION_REFUSED
    # A role somebody switched off is a decision, not a problem.
    assert named(reading, "ROLE_ATLAS").status == CheckStatus.SATISFIED.value
    assert "not enabled" in named(reading, "ROLE_ATLAS").note.lower()


async def test_an_ambiguous_chain_binding_blocks_the_role_that_needs_it(risk_db, now):
    """Two chains and a port that can only serve one is the existing limit."""
    _, sessions = risk_db
    await migrate_marker(sessions)
    settings = preflight_settings(
        market_provider="geckoterminal",
        market_chains="robinhood,bsc",
        vector_worker_enabled=True,
        vector_history_provider="geckoterminal",
        reasoning_provider="anthropic",
    )

    reading = await check(sessions, settings, now)

    vector = named(reading, "ROLE_VECTOR")
    assert vector.status == CheckStatus.BLOCKED.value
    assert vector.reason == "MARKET_HISTORY_CHAIN_AMBIGUOUS"
    assert reading.ready is False


async def test_more_chains_than_the_provider_permits_are_refused(risk_db, now):
    """Judged by the directory's own rule, not by a second list of chains."""
    _, sessions = risk_db
    await migrate_marker(sessions)
    settings = preflight_settings(market_chains="robinhood,bsc", geckoterminal_max_chains=1)

    reading = await check(sessions, settings, now)

    chains = named(reading, "MARKET_CHAINS")
    assert chains.status == CheckStatus.BLOCKED.value
    assert chains.reason == "PROVIDER_CONFIGURATION"
    assert reading.ready is False


async def test_the_budgets_a_run_would_hold_itself_to_are_reported(risk_db, now):
    """Built the way a run builds them, and shown so an operator can read them."""
    _, sessions = risk_db
    await migrate_marker(sessions)
    settings = preflight_settings(
        paper_runner_max_steps=7,
        paper_runner_max_cases=2,
        paper_runner_market_acquisition_enabled=True,
        market_provider="geckoterminal",
        paper_runner_acquisition_max_markets=3,
        paper_runner_acquisition_max_seconds=30,
    )

    reading = await check(sessions, settings, now)

    assert "steps 7" in named(reading, "RUN_BUDGETS").note
    assert "cases 2" in named(reading, "RUN_BUDGETS").note
    acquisition = named(reading, "MARKET_ACQUISITION")
    assert acquisition.status == CheckStatus.SATISFIED.value
    assert "markets 3" in acquisition.note and "30s" in acquisition.note
    assert reading.ready is True


def test_a_budget_combination_that_cannot_exist_is_refused_at_settings():
    """A step that may outlast its run is refused before a check could run."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        preflight_settings(paper_runner_max_seconds=10, paper_runner_step_timeout_seconds=30)
    with pytest.raises(ValidationError):
        preflight_settings(
            paper_runner_market_acquisition_enabled=True,
            market_provider="geckoterminal",
            paper_runner_max_seconds=20,
            paper_runner_acquisition_max_seconds=60,
        )


# --------------------------------------------------------------------- the database


async def test_an_unmigrated_database_is_blocked(risk_db, now):
    """No recorded revision is not "probably fine"."""
    _, sessions = risk_db

    reading = await check(sessions, preflight_settings(), now)

    schema = named(reading, "DATABASE_SCHEMA")
    assert schema.status == CheckStatus.BLOCKED.value
    assert schema.reason == "SCHEMA_NOT_MIGRATED"
    assert expected_revision() in schema.note
    assert reading.exit_code is ExitCode.CONFIGURATION_REFUSED


async def test_an_unexpected_revision_is_blocked(risk_db, now):
    """Behind, ahead and unrelated are all "not what this code expects"."""
    _, sessions = risk_db
    await migrate_marker(sessions, revision="0006")

    reading = await check(sessions, preflight_settings(), now)

    schema = named(reading, "DATABASE_SCHEMA")
    assert schema.status == CheckStatus.BLOCKED.value
    assert schema.reason == "SCHEMA_REVISION_MISMATCH"
    assert "0006" in schema.note and expected_revision() in schema.note


async def test_a_paused_account_is_blocked(risk_db, now):
    """The durable stop, read the way every other reader reads it."""
    from tests.runner.conftest import set_account

    _, sessions = risk_db
    await migrate_marker(sessions)
    await set_account(sessions, paused=True)

    reading = await check(sessions, preflight_settings(), now)

    pause = named(reading, "ACCOUNT_PAUSE")
    assert pause.status == CheckStatus.BLOCKED.value
    assert pause.reason == "ACCOUNT_PAUSED"
    assert reading.ready is False
    assert reading.exit_code is ExitCode.CONFIGURATION_REFUSED


async def test_a_check_that_hangs_is_cut_off_and_reported_as_unavailable(risk_db, now):
    """A check that can hang is not a check.

    Entry and cancellation are proved by events rather than by assuming how fast
    a scheduler gets there, and the outcome is an *unavailable* check rather
    than a verdict: not being able to tell is not the same as not being ready.
    """
    _, sessions = risk_db
    hanging = HangingSessions()
    settings = preflight_settings(paper_runner_step_timeout_seconds=1)
    stack = build_stack(settings, sessions, ports=RunnerPorts(), clock=FixedClock(now))

    reading = await Preflight(settings, hanging, clock=FixedClock(now)).run(stack)

    assert hanging.entered.is_set(), "the read never started"
    assert hanging.cancelled.is_set(), "the hanging read was abandoned rather than cancelled"
    assert named(reading, "DATABASE_SCHEMA").status == CheckStatus.UNAVAILABLE.value
    assert named(reading, "DATABASE_SCHEMA").reason == "DATABASE_TIMEOUT"
    assert named(reading, "ACCOUNT_PAUSE").status == CheckStatus.UNAVAILABLE.value
    assert reading.errors == ("DATABASE_TIMEOUT",)
    assert reading.ready is False
    assert reading.exit_code is ExitCode.TECHNICAL_FAILURE


class HangingSessions:
    """A session factory whose reads never answer, and say when they were cut off."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *arguments) -> bool:
        return False

    async def connection(self):
        return await self._hang()

    async def scalar(self, *arguments, **keywords):
        return await self._hang()

    async def _hang(self):
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("unreachable")  # pragma: no cover


# ------------------------------------------------------------- what it must not do


async def test_a_preflight_changes_no_row(risk_db, now):
    """Read-only, proved against the tables a run would otherwise touch."""
    _, sessions = risk_db
    await migrate_marker(sessions)
    settings = preflight_settings(pulse_worker_enabled=True, fuse_worker_enabled=True)
    before = await counts(sessions)

    reading = await check(sessions, settings, now)

    assert reading.ready is True, reading
    assert await counts(sessions) == before


async def test_a_preflight_makes_no_http_call(risk_db, now, monkeypatch):
    """Composition builds clients. Building one must not reach anybody.

    Every provider this system can compose goes through httpx, so refusing at
    that boundary covers the market provider, the quote source, the indexers,
    the RPC client and the model. The database is asyncpg and is unaffected.
    """
    import httpx

    def forbidden(*arguments, **keywords):
        raise AssertionError("the preflight attempted an HTTP request")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)

    _, sessions = risk_db
    await migrate_marker(sessions)
    settings = preflight_settings(
        market_provider="geckoterminal",
        market_chains="robinhood",
        reasoning_provider="anthropic",
        execution_quote_provider="kyberswap",
        vector_history_provider="geckoterminal",
        evm_runtime_enabled=True,
        rh_chain_enabled=True,
        rh_rpc_http_url="https://rpc.invalid/rh",
        rh_rpc_ws_url="wss://rpc.invalid/rh",
        orbit_worker_enabled=True,
        vector_worker_enabled=True,
        anchor_worker_enabled=True,
    )

    # Composed from settings, exactly as production composes it: every client
    # here is the real one.
    reading = await check(sessions, settings, now, ports=None)

    assert reading.ready is True, reading


async def test_a_preflight_touches_no_port_it_was_given(risk_db, now):
    """Ports are composed and stored. Touching one would raise, and none does."""
    _, sessions = risk_db
    await migrate_marker(sessions)
    settings = preflight_settings(
        orbit_worker_enabled=True,
        reasoning_provider="anthropic",
        anchor_worker_enabled=True,
        atlas_worker_enabled=True,
        evm_runtime_enabled=True,
        signal_worker_enabled=True,
        signal_social_provider="neynar",
        neynar_api_key="unused-the-port-is-supplied",
        vector_worker_enabled=True,
    )
    ports = RunnerPorts(
        reasoning=Exploding(),
        quotes=Exploding(),
        onchain=Exploding(),
        holders=Exploding(),
        origins=Exploding(),
        social=Exploding(),
        history=Exploding(),
    )

    reading = await check(sessions, settings, now, ports=ports)

    assert reading.ready is True, reading
    # Every role this configuration switched on composed, from ports that would
    # have raised had anything asked them a question.
    enabled = {"ORBIT", "ATLAS", "SIGNAL", "VECTOR", "ANCHOR"}
    assert {item.role for item in reading.roles if item.available} >= enabled, reading.roles


class Exploding:
    """Anything asked of this outside its own identity is a failed test."""

    def __getattr__(self, name: str):
        raise AssertionError(f"the preflight used a port: {name}")


async def test_no_configured_value_reaches_the_report(risk_db, now):
    """Codes, counts and sentences written in the source. Nothing else."""
    _, sessions = risk_db
    await migrate_marker(sessions)
    secret = "s3cret-value-that-must-never-be-printed"
    settings = preflight_settings(
        reasoning_provider="anthropic",
        anthropic_api_key=secret,
        signal_social_provider="neynar",
        neynar_api_key=secret,
        atlas_rh_holder_provider="blockscout",
        blockscout_api_key=secret,
        market_provider="geckoterminal",
        market_chains="robinhood",
        evm_runtime_enabled=True,
        rh_chain_enabled=True,
        rh_rpc_http_url=f"https://{secret}@rpc.invalid/rh",
        rh_rpc_ws_url=f"wss://{secret}@rpc.invalid/rh",
    )

    reading = await check(sessions, settings, now)
    rendered = render(reading)

    assert secret not in rendered
    assert "rpc.invalid" not in rendered
    assert settings.database_url not in rendered
    # And the whole document really is only codes, counts and authored notes.
    document = json.loads(rendered)
    assert document["kind"] == "paper_run_preflight"


async def test_a_database_that_cannot_be_reached_never_echoes_its_url(risk_db, now, monkeypatch):
    """A failed connection is a code, never a connection string."""
    from src.runner import preflight as module

    _, sessions = risk_db
    password = "p4ssword-not-for-printing"
    settings = preflight_settings(
        database_url=f"postgresql+asyncpg://runner:{password}@localhost:5432/rh_agents_runner"
    )

    class Refusing:
        def __call__(self):
            raise OSError(f"could not connect to postgresql://runner:{password}@localhost")

    stack = build_stack(settings, sessions, ports=RunnerPorts(), clock=FixedClock(now))
    reading = await module.Preflight(settings, Refusing(), clock=FixedClock(now)).run(stack)
    rendered = render(reading)

    assert password not in rendered
    assert named(reading, "DATABASE_SCHEMA").reason == "DATABASE_UNAVAILABLE"
    assert reading.errors == ("DATABASE_UNAVAILABLE",)
    assert reading.exit_code is ExitCode.TECHNICAL_FAILURE


# -------------------------------------------------------------------------- the CLI


def test_preflight_and_once_cannot_be_asked_for_together(monkeypatch, capsys):
    """Two modes with opposite promises are not one invocation."""
    monkeypatch.setattr("sys.argv", ["runner", "--once", "--preflight"])
    with pytest.raises(SystemExit) as refused:
        main()
    assert refused.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


def test_a_mode_is_required(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["runner"])
    with pytest.raises(SystemExit) as refused:
        main()
    assert refused.value.code == 2
    assert "required" in capsys.readouterr().err


def test_invalid_settings_are_reported_without_echoing_them(monkeypatch, capsys):
    """The preflight mode has its own refusal, and it names nothing."""
    monkeypatch.setattr("sys.argv", ["runner", "--preflight"])
    monkeypatch.setenv("DATABASE_URL", "sqlite:///not-supported")

    assert main() == int(ExitCode.CONFIGURATION_REFUSED)

    printed = json.loads(capsys.readouterr().out)
    assert printed["kind"] == "paper_run_preflight"
    assert printed["ready"] is False
    assert printed["checks"][0]["reason"] == "SETTINGS_INVALID"
    assert "sqlite" not in json.dumps(printed)


async def test_the_cli_mode_runs_the_real_check(risk_db, now, monkeypatch, capsys):
    """The published entry point, over a real database, writing nothing."""
    engine, sessions = risk_db
    await migrate_marker(sessions)

    class EngineView:
        async def dispose(self) -> None:
            return None

    monkeypatch.setattr("src.runner.preflight.connect", lambda url: (EngineView(), sessions))
    settings = preflight_settings(pulse_worker_enabled=True)
    before = await counts(sessions)

    from src.runner.main import check_only

    reading = await check_only(settings)

    assert reading.ready is True, reading
    assert int(reading.exit_code) == 0
    assert await counts(sessions) == before
