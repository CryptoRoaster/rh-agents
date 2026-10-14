"""`--preflight` under PAPER_EARLY_ONLY.

Early-only executes TRADE_CASE_EARLY_V1 and nothing else, so a V2-only role
(VECTOR) switched off is a decision, not a blocker. Every input the early
strategy really needs is still checked by name and never reported as ready
when it is missing. Outside early-only nothing changes.
"""

import pytest

from src.runner.preflight import REQUIRED_ROLE_DISABLED, CheckStatus
from tests.runner.test_preflight import check, migrate_marker, named, preflight_settings

# Everything early-only needs, composable without contacting anybody.
EARLY_ONLY = {
    "vector_worker_enabled": False,
    "signal_worker_enabled": False,
    "signal_social_provider": "disabled",
    "market_provider": "geckoterminal",
    "market_chains": "robinhood",
    "early_scout_enabled": True,
    "atlas_funding_graph_enabled": True,
    "atlas_rh_origin_provider": "blockscout",
    "pre_vector_early_entry_enabled": True,
    "early_paper_exit_enabled": True,
    "vector_history_provider": "geckoterminal",
    "execution_quote_provider": "kyberswap",
    "paper_runner_market_acquisition_enabled": True,
    "paper_entry_requires_exit_job": True,
    "paper_early_only": True,
}

DEPENDENCIES = (
    "EARLY_ONLY_INTAKE",
    "EARLY_ONLY_EXIT_POLICY",
    "EARLY_ONLY_HISTORY",
    "EARLY_ONLY_FUNDING_SOURCE",
    "EARLY_ONLY_QUOTES",
    "EARLY_ONLY_ACQUISITION",
    "EARLY_ONLY_LIVENESS",
)


def early_only_settings(**overrides):
    return preflight_settings(**{**EARLY_ONLY, **overrides})


async def test_a_complete_early_only_configuration_is_ready(risk_db, now):
    _, sessions = risk_db
    await migrate_marker(sessions)

    reading = await check(sessions, early_only_settings(), now)

    assert reading.ready is True, reading.blocked
    vector = named(reading, "ROLE_VECTOR")
    assert vector.status == CheckStatus.SATISFIED.value
    assert "early-only" in vector.note.lower()
    for name in DEPENDENCIES:
        assert named(reading, name).status == CheckStatus.SATISFIED.value, name


async def test_vector_off_still_blocks_outside_early_only(risk_db, now):
    _, sessions = risk_db
    await migrate_marker(sessions)

    reading = await check(sessions, early_only_settings(paper_early_only=False), now)

    assert reading.ready is False
    vector = named(reading, "ROLE_VECTOR")
    assert vector.status == CheckStatus.BLOCKED.value
    assert vector.reason == REQUIRED_ROLE_DISABLED
    # The early-only checks exist only in early-only.
    assert not any(item.name.startswith("EARLY_ONLY_") for item in reading.checks)


@pytest.mark.parametrize("role", ["ORBIT", "ATLAS", "PULSE", "ANCHOR"])
async def test_an_early_workflow_role_off_still_blocks(risk_db, now, role):
    _, sessions = risk_db
    await migrate_marker(sessions)

    reading = await check(
        sessions, early_only_settings(**{f"{role.lower()}_worker_enabled": False}), now
    )

    assert reading.ready is False
    found = named(reading, f"ROLE_{role}")
    assert found.status == CheckStatus.BLOCKED.value
    assert found.reason == REQUIRED_ROLE_DISABLED


@pytest.mark.parametrize(
    ("override", "name", "reason"),
    [
        (
            {"vector_history_provider": "disabled"},
            "EARLY_ONLY_HISTORY",
            "EARLY_ONLY_HISTORY_NOT_CONFIGURED",
        ),
        (
            {"execution_quote_provider": "disabled"},
            "EARLY_ONLY_QUOTES",
            "EARLY_ONLY_QUOTES_NOT_CONFIGURED",
        ),
        (
            {"paper_runner_market_acquisition_enabled": False},
            "EARLY_ONLY_ACQUISITION",
            "EARLY_ONLY_ACQUISITION_DISABLED",
        ),
        (
            {"paper_entry_requires_exit_job": False},
            "EARLY_ONLY_LIVENESS",
            "EARLY_ONLY_LIVENESS_GATE_DISABLED",
        ),
    ],
)
async def test_a_missing_early_dependency_is_never_ready(risk_db, now, override, name, reason):
    _, sessions = risk_db
    await migrate_marker(sessions)

    reading = await check(sessions, early_only_settings(**override), now)

    assert reading.ready is False
    found = named(reading, name)
    assert found.status == CheckStatus.BLOCKED.value
    assert found.reason == reason


async def test_early_only_without_the_funding_source_is_blocked(risk_db, now):
    """S1: no origin provider, no funding source, no creation time, no early case.

    The settings accept the funding graph switched on without an origin
    provider, and ATLAS then composes no funding source at all: every early
    case would be refused EARLY_CREATION_TIME_UNAVAILABLE. Preflight must say
    so by name instead of reporting ready.
    """
    _, sessions = risk_db
    await migrate_marker(sessions)

    reading = await check(sessions, early_only_settings(atlas_rh_origin_provider="disabled"), now)

    assert reading.ready is False
    funding = named(reading, "EARLY_ONLY_FUNDING_SOURCE")
    assert funding.status == CheckStatus.BLOCKED.value
    assert funding.reason == "EARLY_ONLY_FUNDING_SOURCE_NOT_CONFIGURED"
    assert "FUNDING_ORIGIN_PROVIDER_NOT_BLOCKSCOUT" in funding.note


async def test_early_only_with_the_funding_source_names_it_satisfied(risk_db, now):
    _, sessions = risk_db
    await migrate_marker(sessions)

    reading = await check(sessions, early_only_settings(), now)

    assert named(reading, "EARLY_ONLY_FUNDING_SOURCE").status == CheckStatus.SATISFIED.value


@pytest.mark.parametrize(
    ("overrides", "chain", "code"),
    [
        ({}, "robinhood", None),
        (
            {
                "atlas_funding_graph_enabled": False,
                "pre_vector_early_entry_enabled": False,
                "paper_early_only": False,
            },
            "robinhood",
            "FUNDING_GRAPH_NOT_ENABLED",
        ),
        ({}, "bsc", "FUNDING_CHAIN_NOT_SUPPORTED"),
        (
            {"atlas_rh_origin_provider": "disabled"},
            "robinhood",
            "FUNDING_ORIGIN_PROVIDER_NOT_BLOCKSCOUT",
        ),
    ],
)
def test_the_funding_source_conditions_are_stated_once(overrides, chain, code):
    """The factory and the preflight read the same predicate."""
    from src.agents.atlas.sources.factory import funding_refusal, funding_source

    settings = early_only_settings(**overrides)
    assert funding_refusal(settings, chain) == code
    assert (funding_source(settings, chain) is None) is (code is not None)
