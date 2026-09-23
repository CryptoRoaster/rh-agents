"""A real run is reachable only through evidence about *this* configuration.

Three separate things had to be true and were not.

A `PreflightStatus` was free to be any shape, and `all(...)` over an empty
tuple is `True` -- so `PreflightStatus(gates=())` authorised a real run, and so
did one invented `Gate("EVERYTHING", PASS, ...)`. The shape is now checked
before the contents.

An authorization said "something passed" without saying what it passed for, so
a cleared preflight could be paired with a different workspace, a different
isolated home, wider sandbox roots or a different launcher afterwards.

And the client would start a real Codex build with no authorization at all,
which made "the only entry point that may reach a real turn" a convention
rather than a property.

None of these tests starts Codex. The launcher throughout is the fake shim,
declared as one.
"""

import stat
from pathlib import Path

import pytest

from src.evaluation.codex import release as release_module
from src.evaluation.codex import sandbox
from src.evaluation.codex.client import CodexEvaluationClient, binding_for
from src.evaluation.codex.command import (
    CommandBuildError,
    build_version_arguments,
    looks_like_a_native_binary,
)
from src.evaluation.codex.models import CodexLauncher, LauncherKind
from src.evaluation.codex.real_run import RealCodexRunner, RealRunRefused
from src.evaluation.codex.release import (
    ADVISORY_GATES,
    REQUIRED_GATES,
    Gate,
    GateState,
    PreflightStatus,
    ReleaseAuthorization,
    ReleasePermit,
    ReleaseRefused,
    authorize,
    shape_problems,
)
from tests.evaluation.conftest import Probe


def cleared() -> PreflightStatus:
    """Exactly the required gate set, blocking gates PASS, advisory UNVERIFIED."""
    return PreflightStatus(
        gates=tuple(
            Gate(
                name,
                GateState.UNVERIFIED if name in ADVISORY_GATES else GateState.PASS,
                "fixture",
            )
            for name in REQUIRED_GATES
        )
    )


def replacing(name: str, gate: Gate | None) -> PreflightStatus:
    """The cleared set with one gate replaced, or removed when `gate` is None."""
    gates = [item for item in cleared().gates if item.name != name]
    if gate is not None:
        gates.append(gate)
    return PreflightStatus(gates=tuple(gates))


def some_binding(probe: Probe):  # type: ignore[no-untyped-def]
    return binding_for(probe.config(), "0" * 64)


# --------------------------------------------------------------------------
# Blocker 1: the shape of the status
# --------------------------------------------------------------------------


def test_an_empty_status_is_not_a_preflight(probe: Probe) -> None:
    """`all([])` is True, which is how nothing at all used to authorise a run."""
    empty = PreflightStatus(gates=())
    assert empty.may_run is True
    assert empty.blocking == ()
    with pytest.raises(ReleaseRefused) as refused:
        authorize(empty, some_binding(probe))
    assert len(refused.value.problems) == len(REQUIRED_GATES)
    assert "missing gate MODEL_CATALOG" in refused.value.problems


def test_one_invented_passing_gate_is_not_a_preflight(probe: Probe) -> None:
    status = PreflightStatus(gates=(Gate("EVERYTHING", GateState.PASS, "clear"),))
    assert status.may_run is True
    with pytest.raises(ReleaseRefused) as refused:
        authorize(status, some_binding(probe))
    assert "unexpected gate EVERYTHING" in refused.value.problems


def test_a_missing_required_gate_is_refused(probe: Probe) -> None:
    for name in REQUIRED_GATES:
        with pytest.raises(ReleaseRefused) as refused:
            authorize(replacing(name, None), some_binding(probe))
        assert f"missing gate {name}" in refused.value.problems


def test_a_duplicated_required_gate_is_refused(probe: Probe) -> None:
    """Two answers for one question, both PASS, is still not one answer."""
    doubled = PreflightStatus(
        gates=(*cleared().gates, Gate("TOOL_SURFACE", GateState.PASS, "again"))
    )
    with pytest.raises(ReleaseRefused) as refused:
        authorize(doubled, some_binding(probe))
    assert "duplicate gate TOOL_SURFACE" in refused.value.problems


def test_a_substituted_gate_is_refused(probe: Probe) -> None:
    substituted = replacing(
        "OUTER_READ_SANDBOX", Gate("OUTER_READ_SANDBOX_OK", GateState.PASS, "renamed")
    )
    with pytest.raises(ReleaseRefused) as refused:
        authorize(substituted, some_binding(probe))
    assert "missing gate OUTER_READ_SANDBOX" in refused.value.problems
    assert "unexpected gate OUTER_READ_SANDBOX_OK" in refused.value.problems


def test_a_blocking_gate_that_is_unverified_is_refused(probe: Probe) -> None:
    """UNVERIFIED is its own answer, not a soft PASS."""
    status = replacing(
        "CHATGPT_SESSION", Gate("CHATGPT_SESSION", GateState.UNVERIFIED, "not probed")
    )
    assert shape_problems(status) == ()
    with pytest.raises(ReleaseRefused) as refused:
        authorize(status, some_binding(probe))
    assert [gate.name for gate in refused.value.blocking] == ["CHATGPT_SESSION"]


def test_the_advisory_gate_may_be_unverified(probe: Probe) -> None:
    binding = some_binding(probe)
    authorization = authorize(cleared(), binding)
    assert authorization.binding == binding
    assert ADVISORY_GATES == frozenset({"AUTH_REMOTE_VALIDITY"})


def test_a_failing_advisory_gate_still_does_not_block(probe: Probe) -> None:
    """Advisory means advisory: it is never the reason a run is refused."""
    status = replacing(
        "AUTH_REMOTE_VALIDITY", Gate("AUTH_REMOTE_VALIDITY", GateState.FAIL, "unknowable")
    )
    assert authorize(status, some_binding(probe)).status is status


def test_an_authorization_still_refuses_a_hand_made_permit(probe: Probe) -> None:
    """A convention, not a boundary -- and the test says which it is.

    Module privacy in Python does not stop a determined in-process caller. What
    this closes is the accident: constructing one without going through
    `authorize` fails rather than silently producing something that looks valid.
    """
    with pytest.raises(ValueError):
        ReleaseAuthorization(status=cleared(), binding=some_binding(probe), _permit=object())


# --------------------------------------------------------------------------
# Blocker 2: the authorization is bound to the configuration
# --------------------------------------------------------------------------


def bound(probe: Probe, **overrides: object):  # type: ignore[no-untyped-def]
    """A configuration with a real bound profile, and its authorization."""
    profile = sandbox.write_bound_profile(probe.scratch)
    roots = sandbox.SandboxRoots(
        codex_vendor=probe.workspace.parent,
        workspace=probe.workspace,
        codex_home=probe.codex_home,
    )
    settings: dict[str, object] = {
        "outer_sandbox": roots,
        "outer_profile": profile,
        "run_preflight": True,
    }
    settings.update(overrides)
    config = probe.config(**settings)
    authorization = authorize(cleared(), binding_for(config, profile.digest))
    return config, authorization, profile, roots


def test_a_bound_configuration_is_accepted(probe: Probe) -> None:
    config, authorization, _, _ = bound(probe)
    runner = RealCodexRunner(authorization=authorization, config=config)
    assert runner.exec_starts == 0


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("workspace", "workspace"),
        ("codex_home", "codex_home"),
        ("home", "home"),
        ("tmpdir", "tmpdir"),
        ("model", "model"),
    ],
)
def test_a_drifted_field_is_refused_by_name(probe: Probe, field: str, expected: str) -> None:
    config, authorization, profile, roots = bound(probe)
    elsewhere = probe.workspace.parent / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    replacement: object = "gpt-5.5" if field == "model" else elsewhere
    drifted = probe.config(
        outer_sandbox=roots, outer_profile=profile, run_preflight=True, **{field: replacement}
    )
    with pytest.raises(RealRunRefused) as refused:
        RealCodexRunner(authorization=authorization, config=drifted)
    assert expected in refused.value.reason


def test_a_wider_sandbox_workspace_root_is_refused(probe: Probe) -> None:
    """The exact case the binding exists for: same authorization, wider roots."""
    config, authorization, profile, _ = bound(probe)
    wider = sandbox.SandboxRoots(
        codex_vendor=probe.workspace.parent,
        workspace=Path("/"),
        codex_home=probe.codex_home,
    )
    with pytest.raises(RealRunRefused) as refused:
        RealCodexRunner(
            authorization=authorization,
            config=probe.config(outer_sandbox=wider, outer_profile=profile, run_preflight=True),
        )
    assert "sandbox_workspace" in refused.value.reason


def test_a_drifted_installation_id_path_is_refused(probe: Probe) -> None:
    """The second writable path is bound exactly like the first.

    It is a path the sandboxed process may write to, so swapping it after the
    preflight would mean the run writes somewhere the preflight never measured.
    """
    _, authorization, profile, _ = bound(probe)
    other_home = probe.workspace.parent / "swapped-home"
    other_home.mkdir(exist_ok=True)
    swapped = sandbox.SandboxRoots(
        codex_vendor=probe.workspace.parent,
        workspace=probe.workspace,
        codex_home=other_home,
    )
    with pytest.raises(RealRunRefused) as refused:
        RealCodexRunner(
            authorization=authorization,
            config=probe.config(outer_sandbox=swapped, outer_profile=profile, run_preflight=True),
        )
    assert "sandbox_installation_id_file" in refused.value.reason


def test_the_binding_names_both_writable_paths(probe: Probe) -> None:
    config, _, profile, roots = bound(probe)
    binding = binding_for(config, profile.digest)
    assert binding.sandbox_auth_file == str(probe.codex_home.resolve() / "auth.json")
    assert binding.sandbox_installation_id_file == str(
        probe.codex_home.resolve() / "installation_id"
    )
    assert binding.sandbox_auth_file != binding.sandbox_installation_id_file


def test_a_different_sandbox_codex_home_and_auth_file_are_refused(probe: Probe) -> None:
    config, authorization, profile, _ = bound(probe)
    other_home = probe.workspace.parent / "other-home"
    other_home.mkdir(exist_ok=True)
    swapped = sandbox.SandboxRoots(
        codex_vendor=probe.workspace.parent,
        workspace=probe.workspace,
        codex_home=other_home,
    )
    with pytest.raises(RealRunRefused) as refused:
        RealCodexRunner(
            authorization=authorization,
            config=probe.config(outer_sandbox=swapped, outer_profile=profile, run_preflight=True),
        )
    assert "sandbox_codex_home" in refused.value.reason
    assert "sandbox_auth_file" in refused.value.reason
    assert "sandbox_installation_id_file" in refused.value.reason


def test_a_different_launcher_is_refused(probe: Probe) -> None:
    config, authorization, profile, roots = bound(probe)
    other = probe.workspace.parent / "other-codex"
    other.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    other.chmod(other.stat().st_mode | stat.S_IXUSR)
    with pytest.raises(RealRunRefused) as refused:
        RealCodexRunner(
            authorization=authorization,
            config=probe.config(
                outer_sandbox=roots,
                outer_profile=profile,
                run_preflight=True,
                launcher=CodexLauncher(
                    kind=LauncherKind.FAKE_EXECUTABLE, executable=other, path_entries=()
                ),
            ),
        )
    assert "launcher_path" in refused.value.reason


def test_a_different_catalog_digest_is_refused(probe: Probe) -> None:
    config, authorization, profile, roots = bound(probe)
    probe.catalog(slug="gpt-5.4", use_responses_lite=True)
    with pytest.raises(RealRunRefused) as refused:
        RealCodexRunner(
            authorization=authorization,
            config=probe.config(outer_sandbox=roots, outer_profile=profile, run_preflight=True),
        )
    assert "catalog_digest" in refused.value.reason


def test_a_rewritten_profile_is_refused(probe: Probe) -> None:
    """The TOCTOU case: measured here, loaded there, different bytes."""
    config, authorization, profile, _ = bound(probe)
    profile.path.write_text("(version 1)\n(allow default)\n", encoding="utf-8")
    with pytest.raises(RealRunRefused) as refused:
        RealCodexRunner(authorization=authorization, config=config)
    assert refused.value.reason == "profile digest mismatch"


def test_a_removed_profile_is_refused(probe: Probe) -> None:
    config, authorization, profile, _ = bound(probe)
    profile.path.unlink()
    with pytest.raises(RealRunRefused) as refused:
        RealCodexRunner(authorization=authorization, config=config)
    assert refused.value.reason == "profile digest mismatch"


def test_a_configuration_without_an_outer_sandbox_is_refused(probe: Probe) -> None:
    _, authorization, _, _ = bound(probe)
    with pytest.raises(RealRunRefused) as refused:
        RealCodexRunner(authorization=authorization, config=probe.config())
    assert refused.value.reason == "no outer sandbox configured"


def test_a_configuration_with_the_login_probe_off_is_refused(probe: Probe) -> None:
    _, authorization, profile, roots = bound(probe)
    with pytest.raises(RealRunRefused) as refused:
        RealCodexRunner(
            authorization=authorization,
            config=probe.config(outer_sandbox=roots, outer_profile=profile, run_preflight=False),
        )
    assert refused.value.reason == "login preflight disabled"


# --------------------------------------------------------------------------
# Blocker 3: the client will not start a real build on its own
# --------------------------------------------------------------------------


def test_a_permit_cannot_be_built_beside_the_preflight(probe: Probe) -> None:
    """A binding that agrees with itself is not evidence of a preflight.

    This was the remaining hole. A caller holding a configuration can compute
    `binding_for(config, ...)` from that same configuration, so a hand-made
    permit would always match and `_permit_problem` would find nothing to
    object to -- while no preflight had run at all.
    """
    binding = some_binding(probe)
    with pytest.raises(ValueError):
        ReleasePermit(binding=binding)
    with pytest.raises(ValueError):
        ReleasePermit(binding=binding, _token=object())
    # Not even the module's other token, which guards a different type.
    with pytest.raises(ValueError):
        ReleasePermit(binding=binding, _token=release_module._PERMIT)


def test_only_an_authorization_mints_a_permit(probe: Probe) -> None:
    binding = some_binding(probe)
    permit = authorize(cleared(), binding).permit()
    assert isinstance(permit, ReleasePermit)
    assert permit.binding == binding
    # Minting twice gives equal permits rather than a stateful one-shot: the
    # property is where it came from, not how often it was asked for.
    assert authorize(cleared(), binding).permit() == permit


@pytest.mark.asyncio
async def test_a_hand_made_permit_cannot_release_a_real_launcher(probe: Probe) -> None:
    """The whole point, end to end: no preflight, so no process of any kind."""
    config, _, profile, roots = bound(probe)
    real = probe.config(
        launcher=CodexLauncher(
            kind=LauncherKind.PLATFORM_BINARY,
            executable=probe.launcher_path,
            path_entries=(Path("/usr/bin"), Path("/bin")),
        ),
        outer_sandbox=roots,
        outer_profile=profile,
        run_preflight=True,
    )
    # Exactly what a caller could compute from the configuration in hand.
    with pytest.raises(ValueError):
        ReleasePermit(binding=binding_for(real, profile.digest), _token=object())

    # And with the construction refused there is nothing to hand over, so the
    # client is left in the state it refuses from.
    probe.scenario("success")
    client = CodexEvaluationClient(config=real)
    outcome = await client.evaluate(probe.request())
    assert outcome.detail_code == "RELEASE_PERMIT_REQUIRED"  # type: ignore[union-attr]
    assert (client.version_starts, client.preflight_starts, client.exec_starts) == (0, 0, 0)


@pytest.mark.asyncio
async def test_a_minted_permit_for_the_bound_configuration_is_accepted(probe: Probe) -> None:
    """The positive case, so the refusals above are not passing vacuously.

    The launcher is the fake shim declared as a real form, which is the closest
    this suite comes to a real build: the permit path is exercised in full and
    no Codex is started.
    """
    config, _, profile, roots = bound(probe)
    real = probe.config(
        launcher=CodexLauncher(
            kind=LauncherKind.PLATFORM_BINARY,
            executable=probe.launcher_path,
            path_entries=(Path("/usr/bin"), Path("/bin")),
        ),
        outer_sandbox=roots,
        outer_profile=profile,
        run_preflight=True,
    )
    permit = authorize(cleared(), binding_for(real, profile.digest)).permit()
    probe.scenario("success")
    client = CodexEvaluationClient(config=real, permit=permit)
    outcome = await client.evaluate(probe.request())
    # It got past the permit gate and into the ordinary offline path: the
    # version probe ran, which it never does when the permit is refused.
    assert client.version_starts == 1
    assert outcome.detail_code != "RELEASE_PERMIT_REQUIRED"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_a_real_launcher_without_a_permit_starts_nothing(probe: Probe) -> None:
    """The direct bypass. No permit, no process -- of any of the three kinds."""
    probe.scenario("success")
    client = CodexEvaluationClient(
        config=probe.config(
            launcher=CodexLauncher(
                kind=LauncherKind.PLATFORM_BINARY,
                executable=probe.launcher_path,
                path_entries=(Path("/usr/bin"), Path("/bin")),
            ),
            run_preflight=True,
        )
    )
    outcome = await client.evaluate(probe.request())
    assert outcome.detail_code == "RELEASE_PERMIT_REQUIRED"  # type: ignore[union-attr]
    assert (client.version_starts, client.preflight_starts, client.exec_starts) == (0, 0, 0)
    assert not (probe.workspace / "observed-schema.json").exists()


@pytest.mark.asyncio
async def test_a_permit_for_a_different_configuration_starts_nothing(probe: Probe) -> None:
    config, authorization, profile, roots = bound(probe)
    elsewhere = probe.workspace.parent / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    probe.scenario("success")
    client = CodexEvaluationClient(
        config=probe.config(
            launcher=CodexLauncher(
                kind=LauncherKind.PLATFORM_BINARY,
                executable=probe.launcher_path,
                path_entries=(Path("/usr/bin"), Path("/bin")),
            ),
            outer_sandbox=roots,
            outer_profile=profile,
            run_preflight=True,
            home=elsewhere,
        ),
        permit=authorization.permit(),
    )
    outcome = await client.evaluate(probe.request())
    assert outcome.detail_code.startswith("PERMIT_MISMATCH_")  # type: ignore[union-attr]
    assert (client.version_starts, client.preflight_starts, client.exec_starts) == (0, 0, 0)


def test_a_native_binary_may_not_be_declared_a_fake(probe: Probe) -> None:
    """The kind decides whether a permit is needed, so it may not be a free choice.

    `/bin/sh` is compiled on both platforms this runs on -- Mach-O on macOS,
    ELF on Linux -- which is why the magic table lists both families. The check
    is about the launcher kind, and the kind is platform-independent even
    though the sandbox is not.
    """
    assert looks_like_a_native_binary(Path("/bin/sh"))
    disguised = CodexLauncher(
        kind=LauncherKind.FAKE_EXECUTABLE, executable=Path("/bin/sh"), path_entries=()
    )
    with pytest.raises(CommandBuildError) as caught:
        build_version_arguments(launcher=disguised)
    assert caught.value.reason_code == "NATIVE_BINARY_DECLARED_AS_FAKE"

    # And the fixture's own shim, which is a script, stays usable as a fake.
    assert not looks_like_a_native_binary(probe.launcher_path)
    assert build_version_arguments(launcher=probe.launcher())[1:] == ["--version"]
