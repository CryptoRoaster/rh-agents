"""Every Codex invocation goes behind the profile, not only the turn.

The version probe and the login probe were unwrapped for a while, and that was
not a cosmetic gap. `codex login status` opens the credential store, and an
unwrapped probe would read it with the harness's own rights, from whichever
home the environment happened to point at, outside the boundary the attempt
runs in. The two probes also decide whether the attempt may start at all, so a
probe that is not the thing being released is not evidence about it.

`sandbox-exec` is replaced here with a shim that records its arguments and then
execs the command after `--`. That keeps the test about *this* harness's
behaviour -- what it wraps and with which parameters -- rather than about the
macOS sandbox, which `test_sandbox.py` measures for real.
"""

import asyncio
import json
import stat
from pathlib import Path

import pytest

from src.evaluation.codex import sandbox
from src.evaluation.codex.command import AUTH_STORE_OVERRIDE, build_preflight_arguments
from src.evaluation.codex.models import CodexLauncher, EvaluationCompleted, LauncherKind
from tests.evaluation.conftest import Probe

RECORD = "invocations.jsonl"


def write_recording_shim(directory: Path, log: Path) -> Path:
    """A stand-in for sandbox-exec that logs its argv and execs the real command."""
    shim = directory / "recording-sandbox-exec"
    shim.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "' + str(log) + '"\n'
        "while [ \"$1\" != '--' ]; do shift; done\n"
        "shift\n"
        'exec "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def test_the_login_probe_pins_the_credential_store() -> None:
    """Without this the probe could answer from the keychain.

    `cli_auth_credentials_store` defaults to a mode that reaches the login
    keychain, which is shared with the real session and is not in the isolated
    home. The probe would then report a session the attempt cannot use.
    """
    launcher = CodexLauncher(kind=LauncherKind.PLATFORM_BINARY, executable=Path("/usr/bin/true"))
    arguments = build_preflight_arguments(launcher=launcher)
    assert arguments[1:3] == ["login", "status"]
    assert AUTH_STORE_OVERRIDE in arguments
    assert AUTH_STORE_OVERRIDE == 'cli_auth_credentials_store="file"'


@pytest.mark.asyncio
async def test_all_three_invocations_are_wrapped(
    probe: Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = probe.tmpdir / RECORD
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", write_recording_shim(probe.tmpdir, log))

    roots = probe.sandbox_roots()
    probe.scenario("success")
    client = probe.client(outer_sandbox=roots, run_preflight=True)
    outcome = await client.evaluate(probe.request())
    assert isinstance(outcome, EvaluationCompleted)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3, lines
    version, login, attempt = lines
    assert version.endswith("--version")
    assert "login status" in login
    assert AUTH_STORE_OVERRIDE in login
    assert " exec --json " in attempt

    # Each one carries the same four resolved parameters, so no invocation runs
    # under a wider boundary than the others.
    for line in lines:
        for key, value in roots.parameters().items():
            assert f"-D {key}={value}" in line


@pytest.mark.asyncio
async def test_a_configured_sandbox_that_cannot_be_written_refuses_the_attempt(
    probe: Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No silent fallback to an unwrapped Codex.

    A configured outer sandbox that cannot be materialised is a refusal. The
    alternative -- running Codex bare -- would be invisible in the result and
    would be exactly what the configuration exists to prevent.
    """

    def refuse(directory: Path) -> Path:
        raise OSError("no room for a profile")

    monkeypatch.setattr(sandbox, "write_profile", refuse)
    roots = probe.sandbox_roots()
    probe.scenario("success")
    client = probe.client(outer_sandbox=roots, run_preflight=True)
    outcome = await client.evaluate(probe.request())
    assert outcome.detail_code == "OSERROR"  # type: ignore[union-attr]
    assert client.exec_starts == 0
    assert not (probe.workspace / "observed-schema.json").exists()


@pytest.mark.asyncio
async def test_the_probes_and_the_attempt_share_one_profile(
    probe: Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One profile file for the whole attempt, and none left behind."""
    log = probe.tmpdir / RECORD
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", write_recording_shim(probe.tmpdir, log))
    roots = probe.sandbox_roots()
    probe.scenario("success")
    await probe.client(outer_sandbox=roots, run_preflight=True).evaluate(probe.request())

    profiles = set()
    for line in log.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        profiles.add(fields[fields.index("-f") + 1])
    assert len(profiles) == 1
    assert not await asyncio.to_thread(Path(next(iter(profiles))).exists)
    assert await asyncio.to_thread(lambda: sorted(i.name for i in probe.scratch.iterdir())) == []


def test_the_recorded_shape_matches_what_wrap_produces(probe: Probe) -> None:
    """The shim's parsing assumption is the real wrapper's output shape."""
    roots = probe.sandbox_roots()
    wrapped = sandbox.wrap(["codex", "--version"], probe.tmpdir / "profile.sb", roots)
    assert wrapped[0] == str(sandbox.SANDBOX_EXEC)
    assert wrapped[1] == "-f"
    assert wrapped[wrapped.index("--") + 1 :] == ["codex", "--version"]
    assert json.loads(json.dumps(sorted(roots.parameters()))) == [
        "AUTH_FILE",
        "CATALOG_FILE",
        "CODEX_HOME",
        "CODEX_VENDOR",
        "INSTALLATION_ID_FILE",
        "WORKSPACE",
    ]
