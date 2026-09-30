"""`--preflight` against the workflow a PAPER run executes.

A role whose evidence the current workflow (TRADE_CASE_V2) requires, switched off while a PAPER run
is requested, means no case can ever reach risk: `REQUIRED_ROLE_DISABLED`,
blocked, not ready. A role that is on but cannot be built keeps its own source
reason. A configuration that runs the scout alone requests no case, and its
switched-off roles stay decisions.
"""

import pytest

from src.orchestration.workflow.policy import CURRENT_WORKFLOW, TRADE_CASE_V2
from src.runner.preflight import REQUIRED_ROLE_DISABLED, CheckStatus
from tests.runner.test_preflight import (
    bare_settings,
    check,
    migrate_marker,
    named,
    preflight_settings,
)

# TRADE_CASE_V2: SENTIMENT is advisory, so SIGNAL is not among them.
REQUIRED = {
    "ORBIT": "DISCOVERY",
    "ATLAS": "ONCHAIN",
    "VECTOR": "TRADE_SETUP",
    "PULSE": "TRIGGER",
    "ANCHOR": "LIQUIDITY_EXECUTION",
}


def test_the_required_roles_are_the_workflow_s_own():
    assert CURRENT_WORKFLOW is TRADE_CASE_V2
    required = {
        item.role.value: item.evidence_type.name
        for item in CURRENT_WORKFLOW.requirements
        if item.required
    }
    assert required == REQUIRED


async def test_signal_off_and_neynar_missing_do_not_block_a_v2_run(risk_db, now):
    """SIGNAL is optional under V2: off, and with no social key, the run is ready."""
    _, sessions = risk_db
    await migrate_marker(sessions)
    settings = preflight_settings(
        signal_worker_enabled=False, signal_social_provider="disabled", neynar_api_key=""
    )
    reading = await check(sessions, settings, now)
    assert reading.ready is True, reading.blocked
    signal = named(reading, "ROLE_SIGNAL")
    assert signal.status == CheckStatus.SATISFIED.value
    assert "not enabled" in signal.note.lower()


async def test_the_runtime_s_switched_off_required_roles_are_not_ready(risk_db, now):
    """The runtime's configuration today: only PULSE and FUSE on; SIGNAL is optional."""
    _, sessions = risk_db
    await migrate_marker(sessions)
    reading = await check(
        sessions, bare_settings(pulse_worker_enabled=True, fuse_worker_enabled=True), now
    )
    assert reading.ready is False
    for role in ("ORBIT", "ATLAS", "VECTOR", "ANCHOR"):
        found = named(reading, f"ROLE_{role}")
        assert found.status == CheckStatus.BLOCKED.value
        assert found.reason == REQUIRED_ROLE_DISABLED
        assert REQUIRED[role] in found.note
    assert named(reading, "ROLE_PULSE").status == CheckStatus.SATISFIED.value
    assert named(reading, "ROLE_FUSE").status == CheckStatus.SATISFIED.value
    assert named(reading, "ROLE_SIGNAL").status == CheckStatus.SATISFIED.value


@pytest.mark.parametrize("role", sorted(REQUIRED))
async def test_one_required_role_off_blocks_by_name(risk_db, now, role):
    _, sessions = risk_db
    await migrate_marker(sessions)
    settings = preflight_settings(**{f"{role.lower()}_worker_enabled": False})
    reading = await check(sessions, settings, now)
    assert reading.ready is False
    assert [(item.name, item.reason) for item in reading.blocked] == [
        (f"ROLE_{role}", REQUIRED_ROLE_DISABLED)
    ]


async def test_every_required_role_on_and_composable_is_ready(risk_db, now):
    _, sessions = risk_db
    await migrate_marker(sessions)
    reading = await check(sessions, preflight_settings(), now)
    assert reading.ready is True, reading.blocked
    for role in REQUIRED:
        assert named(reading, f"ROLE_{role}").note == "Enabled and composable."


async def test_an_optional_role_off_stays_a_decision(risk_db, now):
    _, sessions = risk_db
    await migrate_marker(sessions)
    reading = await check(sessions, preflight_settings(fuse_worker_enabled=False), now)
    assert reading.ready is True
    assert named(reading, "ROLE_FUSE").status == CheckStatus.SATISFIED.value


async def test_on_but_without_its_source_keeps_the_source_reason(risk_db, now):
    _, sessions = risk_db
    await migrate_marker(sessions)
    settings = preflight_settings(signal_social_provider="disabled", neynar_api_key="")
    reading = await check(sessions, settings, now)
    signal = named(reading, "ROLE_SIGNAL")
    assert signal.status == CheckStatus.BLOCKED.value
    assert signal.reason == "SOCIAL_SOURCE_NOT_CONFIGURED"
    assert reading.ready is False


async def test_a_scout_only_configuration_is_not_held_to_paper_roles(risk_db, now):
    """No PAPER run requested: switched-off roles are decisions, as before."""
    _, sessions = risk_db
    await migrate_marker(sessions)
    settings = bare_settings(
        paper_runner_enabled=False,
        early_scout_enabled=True,
        market_provider="geckoterminal",
        reasoning_provider="anthropic",
    )
    reading = await check(sessions, settings, now)
    reasons = {item.reason for item in reading.checks}
    assert REQUIRED_ROLE_DISABLED not in reasons
    for role in REQUIRED:
        assert named(reading, f"ROLE_{role}").status == CheckStatus.SATISFIED.value
    # The scout's own checks are unchanged, and the run itself is refused by
    # the gate that always refused it.
    assert named(reading, "EARLY_SCOUT_MARKET_PROVIDER").status == CheckStatus.SATISFIED.value
    assert named(reading, "RUN_PERMITTED").reason == "PAPER_RUNNER_NOT_ENABLED"
