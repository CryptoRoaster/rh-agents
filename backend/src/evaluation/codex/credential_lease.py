"""One holder at a time for a Codex login state, across processes.

Codex 0.153.4 rotates ChatGPT refresh tokens. A refresh redeems the stored
refresh token, the provider invalidates it, and the CLI writes the new
generation back into its `auth.json`. Every attempt here runs against an
isolated *copy* of that file, so two attempts that copy the same generation and
both refresh would redeem one refresh token twice; the second is refused as
"already used", and that refusal is permanent until someone signs in again.

So the copy, the preflight, the turn, the write-back of a refreshed copy and the
teardown all happen under one exclusive lease per source `auth.json`. Locking
only the copy would not help: attempt A copies generation 1, releases, attempt
B copies generation 1 too, and whichever refreshes second is refused. B has to
wait until A has either left the source alone or written generation 2 into it.

The lease is an advisory `flock` on a lock file of its own:

* outside the Codex home, in a `0700` directory owned by this uid under
  `/tmp` -- a fixed path rather than `$TMPDIR`, because launchd and an
  interactive shell hand processes different temporary directories, and a lease
  only works if every process asks the same file;
* named after a SHA-256 of the canonical source home path, so the name carries
  no credential and nothing about the login state;
* `0600`, empty, and never removed: unlinking a lock file another process is
  waiting on lets two holders lock two different files.

The kernel drops a `flock` when its descriptor closes, including when the
holder dies, so a crashed attempt cannot leave the lease held. Waiting is
bounded and polls without blocking the event loop; running out of time is a
refusal with a stable code, never a run without the lease.

Nothing here opens, reads or names `auth.json` itself.
"""

import asyncio
import errno
import fcntl
import hashlib
import os
import stat
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

# Fixed on purpose; see the module docstring. Tests pass their own parent.
LEASE_PARENT = Path("/tmp")
LEASE_DIRECTORY_MODE = 0o700
LEASE_FILE_MODE = 0o600
# How long an attempt waits for another one to finish before it gives up. A
# caller with a deadline of its own gets the smaller of the two.
LEASE_WAIT_SECONDS = 30.0
LEASE_POLL_SECONDS = 0.05

LEASE_TIMEOUT = "CODEX_AUTH_LEASE_TIMEOUT"
LEASE_UNAVAILABLE = "CODEX_AUTH_LEASE_UNAVAILABLE"


class CredentialLeaseError(Exception):
    """The lease could not be taken. Carries a stable code and nothing else."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def lease_directory(parent: Path | None = None) -> Path:
    # Read at call time, so a test can point every lease at a directory of its own.
    return (parent or LEASE_PARENT) / f"rh-agents-codex-lease-{os.getuid()}"


def lease_path(source_codex_home: Path, parent: Path | None = None) -> Path:
    """The lock file for one source home: a hash of its path, never its content."""
    canonical = os.path.realpath(source_codex_home)
    digest = hashlib.sha256(canonical.encode("utf-8", errors="surrogateescape")).hexdigest()
    return lease_directory(parent) / f"auth-{digest[:32]}.lock"


def _private_directory(directory: Path) -> None:
    """Create, or accept, a directory only this uid can enter. Anything else refuses."""
    try:
        os.mkdir(directory, LEASE_DIRECTORY_MODE)
    except FileExistsError:
        pass
    except OSError:
        raise CredentialLeaseError(LEASE_UNAVAILABLE) from None
    try:
        info = os.lstat(directory)
    except OSError:
        raise CredentialLeaseError(LEASE_UNAVAILABLE) from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        # A symlink, someone else's directory, or one others can write into:
        # whoever controls it controls who holds the lease.
        raise CredentialLeaseError(LEASE_UNAVAILABLE)


def _open_lock(path: Path) -> int:
    try:
        descriptor = os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, LEASE_FILE_MODE
        )
    except OSError:
        raise CredentialLeaseError(LEASE_UNAVAILABLE) from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise CredentialLeaseError(LEASE_UNAVAILABLE)
        os.fchmod(descriptor, LEASE_FILE_MODE)
    except OSError:
        os.close(descriptor)
        raise CredentialLeaseError(LEASE_UNAVAILABLE) from None
    except CredentialLeaseError:
        os.close(descriptor)
        raise
    return descriptor


def _still_named(path: Path, descriptor: int) -> bool:
    """Whether the locked file is still the one the name points at."""
    try:
        named = os.lstat(path)
        held = os.fstat(descriptor)
    except OSError:
        return False
    return (named.st_dev, named.st_ino) == (held.st_dev, held.st_ino)


async def _acquire(
    path: Path,
    wait_seconds: float,
    poll_seconds: float,
    monotonic: Callable[[], float],
) -> int:
    ends = monotonic() + max(wait_seconds, 0.0)
    while True:
        descriptor = _open_lock(path)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(descriptor)
            if error.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise CredentialLeaseError(LEASE_UNAVAILABLE) from None
            if monotonic() >= ends:
                raise CredentialLeaseError(LEASE_TIMEOUT) from None
            await asyncio.sleep(poll_seconds)
            continue
        if _still_named(path, descriptor):
            return descriptor
        # Locked a file that has since been replaced or removed under its name,
        # so another process could lock the new one at the same time. Start over.
        os.close(descriptor)


@asynccontextmanager
async def credential_lease(
    source_codex_home: Path,
    *,
    wait_seconds: float = LEASE_WAIT_SECONDS,
    parent: Path | None = None,
    poll_seconds: float = LEASE_POLL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> AsyncIterator[Path]:
    """Hold the exclusive lease for `source_codex_home` for the whole block."""
    path = lease_path(source_codex_home, parent)
    _private_directory(path.parent)
    descriptor = await _acquire(path, wait_seconds, poll_seconds, monotonic)
    try:
        yield path
    finally:
        # Closing the descriptor releases the lock; the file itself stays.
        os.close(descriptor)


__all__ = [
    "LEASE_PARENT",
    "LEASE_TIMEOUT",
    "LEASE_UNAVAILABLE",
    "LEASE_WAIT_SECONDS",
    "CredentialLeaseError",
    "credential_lease",
    "lease_directory",
    "lease_path",
]
