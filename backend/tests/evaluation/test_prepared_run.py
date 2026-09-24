"""The environment that was measured is the environment that is held open.

The old preflight deleted its temporary tree before returning, which made its
verdict structurally unable to describe any later run: the isolated home it
judged no longer existed, and whatever ran afterwards ran somewhere else behind
a freshly composed profile. `prepare_real_run` keeps the tree alive for exactly
as long as the authorization is valid, and takes it away on the way out.

The tests that start a real `codex` are skipped unless the supported build is
installed; the ones about the lifecycle and about the profile bytes are not,
because neither needs Codex at all. Nothing here runs a turn.
"""

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from src.evaluation.codex import sandbox
from src.evaluation.codex.client import binding_for
from src.evaluation.codex.final_preflight import default_launcher
from src.evaluation.codex.models import SUPPORTED_CLI_VERSION
from src.evaluation.codex.prepared_run import PLACEHOLDER_AUTH, prepare_real_run
from src.evaluation.codex.release import REQUIRED_GATES

CODEX = shutil.which("codex")


def installed_version() -> str | None:
    if CODEX is None:
        return None
    try:
        result = subprocess.run(
            [CODEX, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment
        return None
    return result.stdout.strip() or result.stderr.strip()


VERSION = installed_version()
SUPPORTED = VERSION is not None and SUPPORTED_CLI_VERSION in VERSION
requires_codex = pytest.mark.skipif(
    not (SUPPORTED and sandbox.available()),
    reason=f"Codex {SUPPORTED_CLI_VERSION} behind a macOS sandbox is not available",
)


def test_the_placeholder_probe_and_the_real_run_share_the_profile_bytes(
    tmp_path: Path,
) -> None:
    """Same policy, different parameters -- which is what makes the probe evidence.

    The boundary is measured against a placeholder home so that no real token
    is pushed through a shell round trip. That is only sound if the placeholder
    run and the real run load the *same* policy text, with the paths supplied
    separately as `-D` values. If the two ever composed differently, the probe
    would be measuring something the run does not use.
    """
    first = sandbox.write_bound_profile(tmp_path)
    second = sandbox.write_bound_profile(tmp_path)
    assert first.digest == second.digest
    assert first.path.read_bytes() == second.path.read_bytes()

    placeholder_roots = sandbox.SandboxRoots(
        codex_vendor=tmp_path,
        workspace=tmp_path / "probe-workspace",
        codex_home=tmp_path / "ph",
        catalog_file=tmp_path / "probe-catalog-runtime" / "models.json",
    )
    real_roots = sandbox.SandboxRoots(
        codex_vendor=tmp_path,
        workspace=tmp_path / "workspace",
        codex_home=tmp_path / "real",
        catalog_file=tmp_path / "catalog-runtime" / "models.json",
    )
    placeholder = sandbox.wrap(["codex"], first.path, placeholder_roots)
    real = sandbox.wrap(["codex"], first.path, real_roots)
    # Everything that differs between the two is a -D value, and nothing else.
    differing = [a for a, b in zip(placeholder, real, strict=True) if a != b]
    assert differing and all(
        item.split("=")[0] in placeholder_roots.parameters() for item in differing
    )


def test_a_rewritten_profile_no_longer_matches(tmp_path: Path) -> None:
    written = sandbox.write_bound_profile(tmp_path)
    assert written.still_matches()
    written.path.write_text("(version 1)\n(allow default)\n", encoding="utf-8")
    assert not written.still_matches()
    written.path.unlink()
    assert not written.still_matches()


@requires_codex
@pytest.mark.asyncio
async def test_the_prepared_run_measures_the_environment_it_holds_open() -> None:
    launcher = default_launcher()
    assert launcher is not None
    async with prepare_real_run(
        launcher=launcher, source_codex_home=Path.home() / ".codex"
    ) as prepared:
        # Exactly the required set, each once. The order is the emission
        # order and is not part of the contract; `shape_problems` does not
        # look at it either.
        names = [gate.name for gate in prepared.status.gates]
        assert sorted(names) == sorted(REQUIRED_GATES)
        assert prepared.runner is not None
        assert prepared.authorization is not None
        assert prepared.runner.exec_starts == 0

        # The tree the verdict is about still exists while the verdict holds.
        assert prepared.config.codex_home.is_dir()
        assert (prepared.config.codex_home / "auth.json").is_file()
        assert prepared.profile.still_matches()

        # And the authorization describes exactly this configuration.
        assert prepared.authorization.binding == binding_for(
            prepared.config, prepared.profile.digest
        )
        root = prepared.root

    assert not root.exists()


def source_state(home: Path) -> dict[str, object]:
    """Existence, size, mode and mtime. Never content.

    `auth.json` is a credential and `installation_id` is a persistent
    identifier; neither belongs in an assertion message, so the fingerprint is
    metadata only.
    """
    state: dict[str, object] = {}
    for name in ("auth.json", "installation_id"):
        path = home / name
        if not path.exists():
            state[name] = "ABSENT"
            continue
        info = path.stat()
        state[name] = (info.st_size, info.st_mode & 0o777, info.st_mtime_ns)
    return state


@requires_codex
@pytest.mark.asyncio
async def test_the_user_codex_home_is_untouched_by_a_preparation() -> None:
    """The source files are read and never written, including the new one.

    A missing `installation_id` is generated in the isolated copy, never
    created in the user's own home -- otherwise a probe run would leave a
    permanent mark on the machine it was only supposed to observe.
    """
    launcher = default_launcher()
    assert launcher is not None
    source = Path.home() / ".codex"
    before = source_state(source)

    async with prepare_real_run(launcher=launcher, source_codex_home=source) as prepared:
        assert prepared.config.codex_home != source
        assert sorted(item.name for item in prepared.config.codex_home.iterdir()) == [
            "auth.json",
            "installation_id",
        ]

    assert source_state(source) == before


@requires_codex
@pytest.mark.asyncio
async def test_the_probe_home_never_holds_the_copied_login_state() -> None:
    """The destructive write check only ever touches a placeholder."""
    launcher = default_launcher()
    assert launcher is not None
    async with prepare_real_run(
        launcher=launcher, source_codex_home=Path.home() / ".codex"
    ) as prepared:
        probe_home = prepared.root / "probe-home"
        assert probe_home.is_dir()
        assert probe_home != prepared.config.codex_home
        # The write check rewrites this file, which is exactly why it is a
        # placeholder and not the copied login state. Trailing whitespace
        # does not survive the shell round trip; the content does.
        written = (probe_home / "auth.json").read_text(encoding="utf-8")
        assert written.strip() == PLACEHOLDER_AUTH.strip()


@requires_codex
@pytest.mark.asyncio
async def test_repeated_preparation_leaves_nothing_behind(tmp_path: Path) -> None:
    """Three cycles, and afterwards no tree, no profile and no stray process."""
    launcher = default_launcher()
    assert launcher is not None
    roots: list[Path] = []
    for _ in range(3):
        async with prepare_real_run(
            launcher=launcher, source_codex_home=Path.home() / ".codex"
        ) as prepared:
            roots.append(prepared.root)
            assert prepared.runner is not None
    assert [root for root in roots if root.exists()] == []

    leftovers = await asyncio.to_thread(
        lambda: list(Path(roots[0]).parent.glob("codex-preflight-*"))
    )
    assert leftovers == []


@requires_codex
@pytest.mark.asyncio
async def test_a_missing_source_home_blocks_the_release() -> None:
    """No login state to copy is a refusal, not a run with an empty home."""
    launcher = default_launcher()
    assert launcher is not None
    async with prepare_real_run(
        launcher=launcher, source_codex_home=Path("/nonexistent-codex-home")
    ) as prepared:
        assert prepared.runner is None
        assert prepared.authorization is None
        blocking = [gate.name for gate in prepared.status.blocking]
        assert "AUTH_HOME_ISOLATION" in blocking
