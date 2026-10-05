"""The credential lifecycle of `prepare_real_run`, without a real Codex.

Every attempt runs under the credential lease from before the copy until after
the teardown, and a login state the CLI rewrote in the isolated home is written
back before that home is removed -- also when the turn fails afterwards. A fake
launcher and refused probes stand in for the CLI; the "refresh" is the test
rewriting the copy the way the 0.153.4 file store does. Every credential here
is a dummy.
"""

import tempfile
from pathlib import Path

import pytest

from src.evaluation.codex import prepared_run, sandbox
from src.evaluation.codex.auth_home import AUTH_FILE, SOURCE_UNSAFE, AuthSourceError, Persistence
from src.evaluation.codex.credential_lease import (
    LEASE_TIMEOUT,
    CredentialLeaseError,
    credential_lease,
)
from src.evaluation.codex.models import CodexLauncher, LauncherKind
from src.evaluation.codex.prepared_run import prepare_real_run

GENERATION_1 = b'{"placeholder": "generation-1"}'
GENERATION_2 = b'{"placeholder": "generation-2", "rotated": true}'


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Where `mkdtemp` puts the attempt trees, so leftovers can be counted."""
    directory = tmp_path / "scratch"
    directory.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(directory))
    return directory


@pytest.fixture
def no_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("no probe may start without time left")

    monkeypatch.setattr(prepared_run, "check_cli_version", refuse)
    monkeypatch.setattr(prepared_run, "check_chatgpt_login", refuse)
    monkeypatch.setattr(sandbox, "probe_boundaries", refuse)


def source_home(tmp_path: Path) -> Path:
    home = tmp_path / "user-home"
    home.mkdir()
    (home / AUTH_FILE).write_bytes(GENERATION_1)
    (home / AUTH_FILE).chmod(0o600)
    (home / "config.toml").write_bytes(b"model = 'placeholder'\n")
    return home


def launcher(tmp_path: Path) -> CodexLauncher:
    executable = tmp_path / "vendor" / "bin" / "codex"
    executable.parent.mkdir(parents=True)
    executable.write_text("")
    return CodexLauncher(kind=LauncherKind.FAKE_EXECUTABLE, executable=executable)


def rewrite_like_codex(path: Path, content: bytes) -> None:
    with open(path, "r+b") as handle:
        handle.truncate(0)
        handle.write(content)


async def assert_lease_free(home: Path) -> None:
    async with credential_lease(home, wait_seconds=0.0):
        pass


def entries(directory: Path) -> list[Path]:
    return list(directory.iterdir())


def assert_clean(home: Path, scratch: Path) -> None:
    assert entries(scratch) == []
    assert sorted(item.name for item in home.iterdir()) == ["auth.json", "config.toml"]
    assert (home / "config.toml").read_bytes() == b"model = 'placeholder'\n"


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_probes")
async def test_the_lease_is_held_for_the_whole_attempt(tmp_path: Path, scratch: Path) -> None:
    home = source_home(tmp_path)
    async with prepare_real_run(
        launcher=launcher(tmp_path), source_codex_home=home, probe_budget_seconds=0.5
    ):
        with pytest.raises(CredentialLeaseError) as caught:
            async with credential_lease(home, wait_seconds=0.1):
                raise AssertionError("a second attempt got in")
        assert caught.value.code == LEASE_TIMEOUT
    await assert_lease_free(home)
    assert_clean(home, scratch)


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_probes")
async def test_an_attempt_without_a_refresh_leaves_the_source_alone(
    tmp_path: Path, scratch: Path
) -> None:
    home = source_home(tmp_path)
    before = (home / AUTH_FILE).stat()
    async with prepare_real_run(
        launcher=launcher(tmp_path), source_codex_home=home, probe_budget_seconds=0.5
    ) as prepared:
        assert prepared.runner is None  # the turn is "rejected": nothing ran
    assert prepared.credentials.persistence is Persistence.UNCHANGED
    after = (home / AUTH_FILE).stat()
    assert (after.st_ino, after.st_mtime_ns, after.st_size) == (
        before.st_ino,
        before.st_mtime_ns,
        before.st_size,
    )
    await assert_lease_free(home)
    assert_clean(home, scratch)


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_probes")
async def test_a_refresh_survives_a_turn_that_fails_afterwards(
    tmp_path: Path, scratch: Path
) -> None:
    """The refresh succeeded, the turn did not: generation 2 is still the only valid one."""
    home = source_home(tmp_path)
    with pytest.raises(RuntimeError, match="turn failed"):
        async with prepare_real_run(
            launcher=launcher(tmp_path), source_codex_home=home, probe_budget_seconds=0.5
        ) as prepared:
            rewrite_like_codex(prepared.config.codex_home / AUTH_FILE, GENERATION_2)
            raise RuntimeError("turn failed after the refresh")
    assert prepared.credentials.persistence is Persistence.PERSISTED
    assert (home / AUTH_FILE).read_bytes() == GENERATION_2
    assert (home / AUTH_FILE).stat().st_mode & 0o777 == 0o600
    await assert_lease_free(home)
    assert_clean(home, scratch)


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_probes")
async def test_a_login_during_the_attempt_is_kept(tmp_path: Path, scratch: Path) -> None:
    home = source_home(tmp_path)
    async with prepare_real_run(
        launcher=launcher(tmp_path), source_codex_home=home, probe_budget_seconds=0.5
    ) as prepared:
        rewrite_like_codex(prepared.config.codex_home / AUTH_FILE, GENERATION_2)
        (home / AUTH_FILE).write_bytes(b'{"placeholder": "signed in by hand"}')
    assert prepared.credentials.persistence is Persistence.SOURCE_CHANGED
    assert (home / AUTH_FILE).read_bytes() == b'{"placeholder": "signed in by hand"}'
    await assert_lease_free(home)
    assert_clean(home, scratch)


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_probes")
async def test_a_failed_write_back_keeps_the_old_file_whole(
    tmp_path: Path, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = source_home(tmp_path)

    def broken(*_args: object) -> None:
        raise OSError(5, "Input/output error")

    with monkeypatch.context() as patched:
        async with prepare_real_run(
            launcher=launcher(tmp_path), source_codex_home=home, probe_budget_seconds=0.5
        ) as prepared:
            rewrite_like_codex(prepared.config.codex_home / AUTH_FILE, GENERATION_2)
            patched.setattr("os.replace", broken)
    assert prepared.credentials.persistence is Persistence.WRITEBACK_FAILED
    assert (home / AUTH_FILE).read_bytes() == GENERATION_1
    await assert_lease_free(home)
    assert_clean(home, scratch)


@pytest.mark.asyncio
async def test_a_failure_before_the_copy_leaves_nothing_and_frees_the_lease(
    tmp_path: Path, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = source_home(tmp_path)

    def broken(*_args: object) -> None:
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patched, pytest.raises(OSError):
        patched.setattr(sandbox, "write_bound_profile", broken)
        async with prepare_real_run(launcher=launcher(tmp_path), source_codex_home=home):
            raise AssertionError("prepared without a profile")
    assert (home / AUTH_FILE).read_bytes() == GENERATION_1
    await assert_lease_free(home)
    assert_clean(home, scratch)


@pytest.mark.asyncio
async def test_an_unsafe_source_refuses_before_anything_runs(tmp_path: Path, scratch: Path) -> None:
    home = tmp_path / "user-home"
    home.mkdir()
    real = tmp_path / "elsewhere.json"
    real.write_bytes(GENERATION_1)
    (home / AUTH_FILE).symlink_to(real)

    with pytest.raises(AuthSourceError) as caught:
        async with prepare_real_run(launcher=launcher(tmp_path), source_codex_home=home):
            raise AssertionError("prepared from a symlinked login state")
    assert caught.value.code == SOURCE_UNSAFE
    assert real.read_bytes() == GENERATION_1
    assert entries(scratch) == []
    await assert_lease_free(home)


@pytest.mark.asyncio
async def test_a_preflight_that_crashes_still_tears_down_and_frees_the_lease(
    tmp_path: Path, scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = source_home(tmp_path)

    async def crash(**_kwargs: object) -> None:
        raise RuntimeError("probe crashed")

    monkeypatch.setattr(prepared_run, "check_cli_version", crash)
    with pytest.raises(RuntimeError, match="probe crashed"):
        async with prepare_real_run(launcher=launcher(tmp_path), source_codex_home=home):
            raise AssertionError("prepared past a crashed probe")
    assert (home / AUTH_FILE).read_bytes() == GENERATION_1
    await assert_lease_free(home)
    assert_clean(home, scratch)


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_probes")
async def test_a_held_lease_refuses_within_the_probe_budget(tmp_path: Path, scratch: Path) -> None:
    """Waiting for the lease draws on the caller's budget: never past its deadline."""
    home = source_home(tmp_path)
    async with credential_lease(home):
        with pytest.raises(CredentialLeaseError) as caught:
            async with prepare_real_run(
                launcher=launcher(tmp_path), source_codex_home=home, probe_budget_seconds=0.3
            ):
                raise AssertionError("prepared while another attempt held the lease")
    assert caught.value.code == LEASE_TIMEOUT
    assert entries(scratch) == []
