"""`--preflight` and ATLAS's holder sources, per chain.

ATLAS composes without a holder provider and would leave HOLDERS unknown on
every case. With a PAPER run requested and ATLAS on, each chain ATLAS reads must
have a holder source the configuration can build; a selected provider without
its key refuses the configuration with a stable code.
"""

import json

from src.core.config import ATLAS_PROVIDER_KEY_MISSING
from src.runner.main import main
from src.runner.models import ExitCode
from src.runner.preflight import ATLAS_HOLDER_SOURCE_NOT_CONFIGURED, CheckStatus
from tests.runner.test_preflight import check, migrate_marker, named, preflight_settings

BSC_RPC = {
    "bsc_chain_enabled": True,
    "bsc_rpc_http_url": "https://bsc.invalid",
    "bsc_rpc_ws_url": "wss://bsc.invalid",
}


async def test_a_bsc_chain_without_a_holder_source_blocks(risk_db, now):
    _, sessions = risk_db
    await migrate_marker(sessions)
    reading = await check(sessions, preflight_settings(**BSC_RPC), now)
    found = named(reading, "ATLAS_HOLDER_SOURCES")
    assert found.status == CheckStatus.BLOCKED.value
    assert found.reason == ATLAS_HOLDER_SOURCE_NOT_CONFIGURED
    assert "bsc" in found.note and "robinhood" not in found.note
    assert reading.ready is False


async def test_bsc_via_nodereal_is_buildable_and_ready(risk_db, now):
    _, sessions = risk_db
    await migrate_marker(sessions)
    settings = preflight_settings(
        **BSC_RPC, atlas_bsc_holder_provider="nodereal", nodereal_api_key="unused-no-call"
    )
    reading = await check(sessions, settings, now)
    found = named(reading, "ATLAS_HOLDER_SOURCES")
    assert found.status == CheckStatus.SATISFIED.value
    assert found.note == "Holder sources: robinhood via blockscout, bsc via nodereal."
    assert reading.ready is True, reading.blocked


async def test_moralis_still_satisfies_the_check(risk_db, now):
    _, sessions = risk_db
    await migrate_marker(sessions)
    settings = preflight_settings(
        **BSC_RPC, atlas_bsc_holder_provider="moralis", moralis_api_key="unused-no-call"
    )
    reading = await check(sessions, settings, now)
    assert named(reading, "ATLAS_HOLDER_SOURCES").status == CheckStatus.SATISFIED.value


async def test_atlas_off_asks_nothing_about_holder_sources(risk_db, now):
    _, sessions = risk_db
    await migrate_marker(sessions)
    reading = await check(sessions, preflight_settings(atlas_worker_enabled=False), now)
    assert "ATLAS_HOLDER_SOURCES" not in {item.name for item in reading.checks}


def test_a_missing_nodereal_key_is_blocked_with_its_code(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["runner", "--preflight"])
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg:///rh_agents_test?host=/tmp")
    monkeypatch.setenv("ATLAS_BSC_HOLDER_PROVIDER", "nodereal")
    monkeypatch.setenv("NODEREAL_API_KEY", "")

    code = main()

    printed = json.loads(capsys.readouterr().out)
    assert code == int(ExitCode.CONFIGURATION_REFUSED)
    assert printed["ready"] is False
    assert printed["checks"][0]["status"] == CheckStatus.BLOCKED.value
    assert printed["checks"][0]["reason"] == ATLAS_PROVIDER_KEY_MISSING
