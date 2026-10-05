"""One holder at a time for a Codex login state -- across real processes.

Codex rotates refresh tokens, so two attempts holding copies of the same
generation would redeem one refresh token twice and the provider would refuse
the second for good. These tests start separate Python processes, because an
asyncio test in one process proves nothing about two scheduler runs.

Every credential is a dummy.
"""

import asyncio
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.evaluation.codex.auth_home import AUTH_FILE
from src.evaluation.codex.credential_lease import (
    LEASE_TIMEOUT,
    LEASE_UNAVAILABLE,
    CredentialLeaseError,
    credential_lease,
    lease_directory,
    lease_path,
)

BACKEND = Path(__file__).resolve().parents[2]
GENERATION_1 = b'{"placeholder": "generation-1"}'
GENERATION_2 = b'{"placeholder": "generation-2"}'


def source_home(tmp_path: Path) -> Path:
    home = tmp_path / "user-home"
    home.mkdir()
    (home / AUTH_FILE).write_bytes(GENERATION_1)
    return home


def start(
    role: str, home: Path, parent: Path, signals: Path, wait: float = 30.0
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tests.evaluation.lease_worker",
            role,
            str(home),
            str(parent),
            str(signals),
            str(wait),
        ],
        cwd=BACKEND,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def wait_for(path: Path, timeout: float = 30.0) -> None:
    ends = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < ends, f"no {path.name} within {timeout}s"
        time.sleep(0.01)


def test_the_lock_file_names_no_credential_and_is_private(tmp_path: Path) -> None:
    home = source_home(tmp_path)
    path = lease_path(home, tmp_path)
    assert path.parent == lease_directory(tmp_path)
    assert path.parent.name == f"rh-agents-codex-lease-{os.getuid()}"
    # A hash of the canonical path: nothing from the login state, and not
    # inside the Codex home either.
    assert path.name.startswith("auth-") and path.name.endswith(".lock")
    assert len(path.name) == len("auth-") + 32 + len(".lock")
    assert home not in path.parents
    assert lease_path(home / ".", tmp_path) == path

    async def take() -> None:
        async with credential_lease(home, parent=tmp_path):
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            assert path.stat().st_size == 0

    asyncio.run(take())
    # The lock file stays; the login state was never touched.
    assert path.exists()
    assert (home / AUTH_FILE).read_bytes() == GENERATION_1


def test_a_second_holder_waits_a_bounded_time_and_is_refused(tmp_path: Path) -> None:
    home = source_home(tmp_path)

    async def contend() -> float:
        async with credential_lease(home, parent=tmp_path):
            started = time.monotonic()
            with pytest.raises(CredentialLeaseError) as caught:
                async with credential_lease(home, parent=tmp_path, wait_seconds=0.3):
                    raise AssertionError("two holders at once")
            assert caught.value.code == LEASE_TIMEOUT
            assert str(caught.value) == LEASE_TIMEOUT
            return time.monotonic() - started

    waited = asyncio.run(contend())
    assert 0.25 <= waited < 5.0


def test_the_lease_is_free_again_after_an_exception(tmp_path: Path) -> None:
    home = source_home(tmp_path)

    async def fail_then_take() -> None:
        with pytest.raises(RuntimeError):
            async with credential_lease(home, parent=tmp_path):
                raise RuntimeError("the attempt failed")
        async with credential_lease(home, parent=tmp_path, wait_seconds=0.0):
            pass

    asyncio.run(fail_then_take())


def test_different_homes_do_not_wait_for_each_other(tmp_path: Path) -> None:
    first = source_home(tmp_path)
    second = tmp_path / "other-home"
    second.mkdir()

    async def both() -> None:
        async with credential_lease(first, parent=tmp_path):
            async with credential_lease(second, parent=tmp_path, wait_seconds=0.0):
                pass

    asyncio.run(both())


@pytest.mark.parametrize("problem", ["symlink", "shared", "file"])
def test_an_untrustworthy_lease_directory_refuses(tmp_path: Path, problem: str) -> None:
    home = source_home(tmp_path)
    parent = tmp_path / "parent"
    parent.mkdir()
    directory = lease_directory(parent)
    if problem == "symlink":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        directory.symlink_to(elsewhere)
    elif problem == "shared":
        directory.mkdir()
        directory.chmod(0o777)
    else:
        directory.write_bytes(b"")

    async def take() -> None:
        async with credential_lease(home, parent=parent, wait_seconds=0.0):
            raise AssertionError("leased through an untrustworthy directory")

    with pytest.raises(CredentialLeaseError) as caught:
        asyncio.run(take())
    assert caught.value.code == LEASE_UNAVAILABLE


def test_a_waiting_process_receives_the_generation_the_holder_wrote_back(
    tmp_path: Path,
) -> None:
    """A refreshes and writes back under the lease; B, waiting, must copy generation 2.

    B is started while A holds the lease and has not refreshed yet. If B could
    copy before A released, it would receive generation 1 -- the refresh token A
    is about to redeem -- and its own refresh would be refused as already used.
    """
    home = source_home(tmp_path)
    parent = tmp_path / "leases"
    parent.mkdir()
    signals = tmp_path / "signals"
    signals.mkdir()

    holder = start("refresher", home, parent, signals)
    try:
        wait_for(signals / "holding")
        reader = start("reader", home, parent, signals)
        try:
            wait_for(signals / "waiting")
            # B is running and blocked: give it every chance to cheat.
            time.sleep(0.5)
            assert not (signals / "received").exists()
            assert not (signals / "attempt-reader").exists()

            (signals / "go").write_bytes(b"")
            assert holder.wait(timeout=30) == 0
            assert (signals / "persisted").read_bytes() == b"PERSISTED"
            assert reader.wait(timeout=30) == 0
        finally:
            reader.kill()
            reader.wait()
    finally:
        holder.kill()
        holder.wait()

    assert (home / AUTH_FILE).read_bytes() == GENERATION_2
    assert (signals / "received").read_bytes() == GENERATION_2


def test_a_process_waiting_too_long_is_refused_not_let_in(tmp_path: Path) -> None:
    home = source_home(tmp_path)
    parent = tmp_path / "leases"
    parent.mkdir()
    signals = tmp_path / "signals"
    signals.mkdir()

    holder = start("holder", home, parent, signals)
    try:
        wait_for(signals / "holding")
        reader = start("reader", home, parent, signals, wait=0.5)
        assert reader.wait(timeout=30) == 3
        assert (signals / "refused").read_bytes() == LEASE_TIMEOUT.encode()
        assert not (signals / "received").exists()
    finally:
        holder.kill()
        holder.wait()


def test_a_killed_holder_does_not_keep_the_lease(tmp_path: Path) -> None:
    """The kernel drops the lock with the process: a crash never wedges the scheduler."""
    home = source_home(tmp_path)
    parent = tmp_path / "leases"
    parent.mkdir()
    signals = tmp_path / "signals"
    signals.mkdir()

    holder = start("holder", home, parent, signals)
    wait_for(signals / "holding")
    holder.send_signal(signal.SIGKILL)
    holder.wait(timeout=30)

    async def take() -> None:
        async with credential_lease(home, parent=parent, wait_seconds=5.0):
            pass

    asyncio.run(take())
    assert (home / AUTH_FILE).read_bytes() == GENERATION_1
