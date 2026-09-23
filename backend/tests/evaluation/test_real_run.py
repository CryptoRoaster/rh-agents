"""A real run is reachable only through evidence, never through a flag.

The earlier `RunMode` was a boolean the caller set, and a boolean the caller
sets is a promise rather than a check. What replaced it is a type: the runner
takes a `ReleaseAuthorization`, `authorize` is the only thing that makes one,
and it makes one only from a preflight where nothing is blocking.

So these tests try the three ways a caller could get around it -- forging the
authorization, taking one from a failing preflight, and holding a real one
while handing over a configuration the preflight never described -- and in each
case they check that no `codex exec` was started.
"""

import pytest

from src.evaluation.codex import release
from src.evaluation.codex.real_run import RealCodexRunner, RealRunRefused
from src.evaluation.codex.release import (
    Gate,
    GateState,
    PreflightStatus,
    ReleaseAuthorization,
    ReleaseRefused,
    authorize,
)
from tests.evaluation.conftest import Probe

CLEAR = PreflightStatus(gates=(Gate("EVERYTHING", GateState.PASS, "clear"),))


def test_an_authorization_cannot_be_constructed_by_hand() -> None:
    with pytest.raises(ValueError):
        ReleaseAuthorization(status=CLEAR, _permit=object())


def test_a_blocked_preflight_yields_no_authorization() -> None:
    blocked = PreflightStatus(
        gates=(
            Gate("OUTER_READ_SANDBOX", GateState.FAIL, "no outer sandbox configured"),
            Gate("CATALOG_DIGEST", GateState.PASS, "pinned digest matches"),
        )
    )
    with pytest.raises(ReleaseRefused) as refused:
        authorize(blocked)
    assert [gate.name for gate in refused.value.blocking] == ["OUTER_READ_SANDBOX"]


def test_an_unverified_gate_blocks_as_firmly_as_a_failure() -> None:
    """UNVERIFIED is not a soft FAIL, and only the advisory gate is exempt."""
    unverified = PreflightStatus(
        gates=(Gate("CHATGPT_SESSION", GateState.UNVERIFIED, "login not probed"),)
    )
    with pytest.raises(ReleaseRefused):
        authorize(unverified)

    advisory = PreflightStatus(
        gates=(
            Gate("AUTH_REMOTE_VALIDITY", GateState.UNVERIFIED, "advisory only"),
            Gate("CHATGPT_SESSION", GateState.PASS, "ChatGPT session reported"),
        )
    )
    assert authorize(advisory).status is advisory


def test_the_advisory_gate_is_the_only_exemption() -> None:
    assert release.ADVISORY_GATES == frozenset({"AUTH_REMOTE_VALIDITY"})


def test_the_runner_refuses_a_configuration_without_the_outer_sandbox(probe: Probe) -> None:
    """A cleared preflight does not authorise a differently shaped attempt.

    The preflight measures a profile. A configuration carrying no profile is
    not the thing that was measured, and the authorization does not stretch to
    cover it.
    """
    with pytest.raises(RealRunRefused) as refused:
        RealCodexRunner(authorization=authorize(CLEAR), config=probe.config())
    assert refused.value.reason == "no outer sandbox configured"


def test_the_runner_refuses_a_configuration_with_the_login_probe_off(probe: Probe) -> None:
    from src.evaluation.codex import sandbox

    roots = sandbox.SandboxRoots(
        codex_vendor=probe.workspace, workspace=probe.workspace, codex_home=probe.codex_home
    )
    with pytest.raises(RealRunRefused) as refused:
        RealCodexRunner(
            authorization=authorize(CLEAR),
            config=probe.config(outer_sandbox=roots, run_preflight=False),
        )
    assert refused.value.reason == "login preflight disabled"


@pytest.mark.asyncio
async def test_a_refused_runner_starts_nothing(probe: Probe) -> None:
    """The refusal happens before a client exists, so there is nothing to start."""
    probe.scenario("success")
    with pytest.raises(RealRunRefused):
        RealCodexRunner(authorization=authorize(CLEAR), config=probe.config())
    assert not (probe.workspace / "observed-schema.json").exists()
