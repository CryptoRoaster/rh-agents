"""Three ways the readiness report could be right by accident.

Each of these first existed as a reproduction against
`25e97a0fff53f66e96b162943051047ebbf63724`, where it failed. The production
check path throughout: the real schema contract, the real CLI entry point and
the real provider composition.
"""

import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from src.data.schema import expected_revision
from src.runner.main import main
from src.runner.models import ExitCode
from src.runner.preflight import CheckStatus
from tests.runner.test_preflight import check, named, preflight_settings

OTHER = "0006"


async def record_revisions(sessions, *revisions):
    """Put exactly these rows in the version table, in this order."""
    async with sessions.begin() as session:
        await session.execute(
            text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        await session.execute(text("DELETE FROM alembic_version"))
        for revision in revisions:
            await session.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": revision})


# ------------------------------------------------- 1. the whole recorded revision set


@pytest.mark.parametrize(
    "recorded,reason",
    [
        ((), "SCHEMA_NOT_MIGRATED"),
        ((OTHER,), "SCHEMA_REVISION_MISMATCH"),
        # Both orders, because reading one row and calling it the answer is
        # exactly what made this depend on which row came back first.
        ((OTHER, "HEAD"), "SCHEMA_MULTIPLE_REVISIONS"),
        (("HEAD", OTHER), "SCHEMA_MULTIPLE_REVISIONS"),
    ],
)
async def test_a_revision_set_that_is_not_exactly_the_head_is_refused(
    risk_db, now, recorded, reason
):
    """A database matches only when its whole recorded set is the one head.

    Reproduction: the recorded revision was read with `.first()`, so a database
    carrying the expected head *and* something else answered with whichever row
    the engine happened to return — and in one insertion order that was the
    expected head, which made an incoherent database report as current.
    """
    _, sessions = risk_db
    await record_revisions(
        sessions, *[expected_revision() if item == "HEAD" else item for item in recorded]
    )

    reading = await check(sessions, preflight_settings(), now)

    schema = named(reading, "DATABASE_SCHEMA")
    assert schema.status == CheckStatus.BLOCKED.value, schema
    assert schema.reason == reason, schema
    assert reading.ready is False
    assert reading.exit_code is ExitCode.CONFIGURATION_REFUSED
    # A refusal is never a repair: the rows are left exactly as they were.
    async with sessions() as session:
        rows = (await session.scalars(text("SELECT version_num FROM alembic_version"))).all()
    assert len(rows) == len(recorded)


async def test_exactly_the_expected_head_is_accepted(risk_db, now):
    _, sessions = risk_db
    await record_revisions(sessions, expected_revision())

    reading = await check(sessions, preflight_settings(), now)

    assert named(reading, "DATABASE_SCHEMA").status == CheckStatus.SATISFIED.value
    assert reading.ready is True, reading


@pytest.mark.parametrize(
    "recorded,ready",
    [
        ((), False),
        ((OTHER,), False),
        ((OTHER, "HEAD"), False),
        (("HEAD", OTHER), False),
        (("HEAD",), True),
    ],
)
async def test_the_ready_endpoint_agrees_with_the_preflight(
    risk_db, now, monkeypatch, recorded, ready
):
    """One schema contract, two callers, the same verdict in every case."""
    from src.core.config import Settings

    # The API module builds an app at import time, which needs a database URL
    # present in the environment. Set before the import, as the market API
    # suite does.
    monkeypatch.setenv("DATABASE_URL", preflight_settings().database_url)
    from src.api.main import create_app

    engine, sessions = risk_db
    await record_revisions(
        sessions, *[expected_revision() if item == "HEAD" else item for item in recorded]
    )

    class EngineView:
        def connect(self):
            return engine.connect()

        async def dispose(self) -> None:
            return None

    monkeypatch.setattr("src.api.main.connect", lambda url: (EngineView(), sessions))
    app = create_app(Settings(_env_file=None))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        status = (await client.get("/ready")).status_code

    reading = await check(sessions, preflight_settings(), now)
    agreed = named(reading, "DATABASE_SCHEMA").status == CheckStatus.SATISFIED.value

    assert (status == 200) is ready
    assert agreed is ready


# --------------------------------------------- 2. a technical failure is not a refusal


def test_a_startup_failure_is_reported_as_unavailable(monkeypatch, capsys):
    """The JSON and the process code must describe the same event.

    Reproduction: a failure to start the check printed a *blocked* check with no
    errors — a report whose own contract says exit 2 — while the process
    returned 1. An operator reading the document and an operator reading `$?`
    were told different things.
    """
    from src.runner import main as module

    monkeypatch.setattr("sys.argv", ["runner", "--preflight"])
    monkeypatch.setenv("DATABASE_URL", preflight_settings().database_url)

    async def unreachable(*arguments, **keywords):
        raise OSError("could not connect to postgresql://runner:s3cret@localhost:5432/db")

    monkeypatch.setattr(module, "check_only", unreachable)

    code = main()

    printed = json.loads(capsys.readouterr().out)
    assert code == int(ExitCode.TECHNICAL_FAILURE)
    assert printed["ready"] is False
    assert printed["errors"] == ["PREFLIGHT_STARTUP_FAILED"]
    assert [item["status"] for item in printed["checks"]] == [CheckStatus.UNAVAILABLE.value]
    assert printed["checks"][0]["reason"] == "PREFLIGHT_STARTUP_FAILED"
    # The document's own contract and the process agree.
    assert _exit_of(printed) == code
    # And nothing of the failure's text survived.
    document = json.dumps(printed)
    assert "s3cret" not in document and "postgresql" not in document


def test_an_interrupted_check_is_reported_as_unavailable(monkeypatch, capsys):
    """Somebody pressed ctrl-c. That is not a verdict about the configuration."""
    from src.runner import main as module

    monkeypatch.setattr("sys.argv", ["runner", "--preflight"])
    monkeypatch.setenv("DATABASE_URL", preflight_settings().database_url)

    async def interrupted(*arguments, **keywords):
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "check_only", interrupted)

    code = main()

    printed = json.loads(capsys.readouterr().out)
    assert code == int(ExitCode.TECHNICAL_FAILURE)
    assert printed["errors"] == ["PREFLIGHT_INTERRUPTED"]
    assert printed["checks"][0]["status"] == CheckStatus.UNAVAILABLE.value
    assert _exit_of(printed) == code


def test_a_real_configuration_error_stays_a_refusal(monkeypatch, capsys):
    """The control: a missing setting is still blocked, and still exits two."""
    monkeypatch.setattr("sys.argv", ["runner", "--preflight"])
    monkeypatch.setenv("DATABASE_URL", "sqlite:///not-supported")

    code = main()

    printed = json.loads(capsys.readouterr().out)
    assert code == int(ExitCode.CONFIGURATION_REFUSED)
    assert printed["errors"] == []
    assert printed["checks"][0]["status"] == CheckStatus.BLOCKED.value
    assert printed["checks"][0]["reason"] == "SETTINGS_INVALID"
    assert _exit_of(printed) == code


def _exit_of(document: dict) -> int:
    """The exit code the printed report itself implies."""
    from src.runner.preflight import PreflightReading

    return int(PreflightReading.model_validate(document).exit_code)


# ------------------------------------------------ 3. one selected fact source is enough


@pytest.mark.parametrize(
    "configured",
    [
        {"atlas_rh_holder_provider": "blockscout", "blockscout_api_key": "unused"},
        {"atlas_bsc_holder_provider": "moralis", "moralis_api_key": "unused"},
        {"atlas_rh_origin_provider": "blockscout", "blockscout_api_key": "unused"},
        {"atlas_bsc_origin_provider": "etherscan", "etherscan_api_key": "unused"},
        {"signal_social_provider": "neynar", "neynar_api_key": "unused"},
    ],
)
async def test_one_configured_fact_source_is_reported_as_unverifiable(
    risk_db, now, monkeypatch, configured
):
    """Any selected indexer or social source is one this check cannot reach.

    Reproduction: the condition asked whether "disabled" was absent from the set
    of all four ATLAS providers, which is only true when every one of them is
    configured. A deployment with a single holder source was reported as having
    nothing external to verify.
    """
    import httpx

    def forbidden(*arguments, **keywords):
        raise AssertionError("the preflight attempted an HTTP request")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)

    _, sessions = risk_db
    await record_revisions(sessions, expected_revision())

    reading = await check(sessions, preflight_settings(**configured), now)

    source = named(reading, "FACT_SOURCE_REACHABLE")
    assert source.status == CheckStatus.NOT_CHECKED.value
    assert source.reason == "REQUIRES_EXTERNAL_CALL"


async def test_no_fact_source_means_nothing_to_report(risk_db, now, monkeypatch):
    """Nothing selected is nothing to say, rather than a warning nobody needs."""
    import httpx

    def forbidden(*arguments, **keywords):
        raise AssertionError("the preflight attempted an HTTP request")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)

    _, sessions = risk_db
    await record_revisions(sessions, expected_revision())

    reading = await check(sessions, preflight_settings(), now)

    assert "FACT_SOURCE_REACHABLE" not in {item.name for item in reading.checks}
    assert reading.ready is True, reading
