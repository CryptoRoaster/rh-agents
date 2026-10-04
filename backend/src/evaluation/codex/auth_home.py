"""A CODEX_HOME that holds the login state and the one file Codex insists on.

Pointing the attempt at the user's own `~/.codex` would mean the outer sandbox
has to make that whole directory readable: config, history, sessions, skills,
plugins, cached models, whatever else has accumulated. None of it is needed for
one structured turn, and all of it would be inside the boundary.

So the harness builds its own: a `0700` directory containing a `0600` copy of
the one file Codex 0.153.4 reads for a ChatGPT session, `auth.json`, and a
`0644` `installation_id`. Nothing else is copied, the directory is removed
afterwards, and `--ignore-user-config` plus `--ephemeral` keep the CLI from
reaching for or leaving anything more.

The second file is not a choice. `codex exec` starts an in-process app-server
client, whose `start_uninitialized` calls `resolve_installation_id`
(`core/src/installation_id.rs`), which opens `CODEX_HOME/installation_id`
read+write+create, locks it, repairs its mode and may rewrite it. With a home
holding only `auth.json` that fails, and the second authorised real probe
reported exactly that: `failed to initialize in-process app-server client:
Operation not permitted (os error 1)`, exit 1, no events.

It is **pre-seeded here rather than left for Codex to create**. Letting the CLI
create it would mean making the directory writable, and a writable directory
means arbitrary sidecar files, an unknown file set, and an `AUTH_HOME_ISOLATION`
gate with nothing left to check. Seeding it keeps the directory unwritable and
the contents known in advance, so exactly one more literal path is opened for
writing and nothing else can appear beside it.

`installation_id` is not a credential -- it is a persistent installation
identifier -- but it is still never read into this process as a value, logged or
reported.

It never parses the login state. The copy goes through the filesystem, byte
for byte, and no value from it is parsed, logged, asserted on or reported --
not on the way in and not on the way back.

**A refresh has to come back.** Codex 0.153.4 rotates ChatGPT refresh tokens:
a refresh redeems the stored refresh token, the provider invalidates it, and
the CLI rewrites its `auth.json` (truncate, write) with the new generation.
This module used to discard that copy, on the theory that the user's own
session stayed untouched. It did stay untouched -- holding a refresh token the
provider had already invalidated. Every later attempt copied that spent token,
was refused with "refresh token was already used", and ORBIT stopped until
someone signed in again.

So the copy is now a lease on the login state rather than a snapshot of it:

* The source `auth.json` is opened without following links, must be a regular
  file, and its metadata -- device, inode, mode, size, mtime and ctime, never
  content -- is the `source_state` the copy was taken from. A symlink or any
  other kind of file refuses (`CODEX_AUTH_SOURCE_UNSAFE`).
* The copy's timestamps are pinned to a fixed sentinel, and its metadata is
  recorded. Any rewrite by the CLI moves its mtime and ctime off that record,
  so a rewrite is recognised from file properties alone, including one that
  leaves the size unchanged.
* `persist_refresh` runs at teardown, whatever the turn did -- a refresh that
  succeeded before a later failure is still a new generation that must not be
  lost. It writes back only `auth.json`, only when the copy changed, and only
  when the source is still exactly the file the copy was taken from. A source
  that changed meanwhile (a manual login or logout, another Codex) wins, and
  the attempt reports `CODEX_AUTH_SOURCE_CHANGED` instead of overwriting it.
* The write-back is a private `0600` sibling, filled byte for byte, fsynced,
  re-checked against the source state and then moved over the source with
  `os.replace`; the source is always either the complete old file or the
  complete new one.

The source home's other files -- `config.toml`, history, sessions,
`installation_id` -- are never written. Callers hold the credential lease
(`credential_lease.py`) around all of this, so no two attempts ever hold the
same generation.

Two questions are kept apart here, because folding them together once produced
a gate nothing could ever clear. Whether the home is *isolated* is checkable
offline and is `AUTH_HOME_ISOLATION`: the directory exists, holds exactly this
one file, and both modes are restrictive. Whether the copied state actually
authenticates against the provider is not checkable without a request, and it
is `AUTH_REMOTE_VALIDITY`, which is advisory and never blocks.

Between the two sits `CHATGPT_SESSION`, which is neither a guess nor a round
trip: the CLI is asked, in this home, behind the outer profile, with the
credential store pinned to `file` so the answer is about this copy rather than
about a keychain entry.
"""

import os
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# `auth.json` is where 0.153.4 keeps the ChatGPT session under the `file`
# credential store, which every invocation pins explicitly rather than leaving
# to the default.
AUTH_FILE = "auth.json"
# The app-server startup file. 0644 is the mode `resolve_installation_id`
# checks for and repairs, so seeding it at anything else would make the CLI
# attempt a chmod on the first run.
INSTALLATION_ID_FILE = "installation_id"

# The complete, closed set. A third entry is an isolation failure.
EXPECTED_ENTRIES = (AUTH_FILE, INSTALLATION_ID_FILE)

HOME_MODE = 0o700
AUTH_MODE = 0o600
INSTALLATION_ID_MODE = 0o644

# Both timestamps of the copy are pinned here after it is written. The CLI
# rewrites the file in place, which stamps the current time, so a rewrite can
# never leave the mtime at this value -- whatever the size, and whatever the
# filesystem's timestamp resolution.
COPY_SENTINEL_NS = 1_000_000_000_000_000_007
# A login state is a few kilobytes. Anything larger is not written back.
MAX_AUTH_BYTES = 1 << 20
WRITEBACK_PREFIX = ".auth.json.rh-agents-"
WRITEBACK_SUFFIX = ".tmp"

SOURCE_UNSAFE = "CODEX_AUTH_SOURCE_UNSAFE"
SOURCE_CHANGED = "CODEX_AUTH_SOURCE_CHANGED"


class AuthSourceError(Exception):
    """The source login state cannot be copied safely. A stable code, no path."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FileState:
    """What is known about a credential file without opening its content."""

    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def of(cls, info: os.stat_result) -> "FileState":
        return cls(
            device=info.st_dev,
            inode=info.st_ino,
            mode=info.st_mode,
            size=info.st_size,
            mtime_ns=info.st_mtime_ns,
            ctime_ns=info.st_ctime_ns,
        )


def file_state(path: Path) -> FileState | None:
    """The state of `path` itself, never of a link target. None when absent."""
    try:
        return FileState.of(os.lstat(path))
    except FileNotFoundError:
        return None


class Persistence(StrEnum):
    """What happened to the copy's login state at teardown."""

    # No login state was copied, so there is nothing that could come back.
    NOT_APPLICABLE = "NOT_APPLICABLE"
    # The CLI did not rewrite the copy; the source is still current.
    UNCHANGED = "UNCHANGED"
    # The CLI rewrote the copy, and that generation replaced the source.
    PERSISTED = "PERSISTED"
    # The source changed while the lease was held; it was left as it is.
    SOURCE_CHANGED = "SOURCE_CHANGED"
    # The rewritten copy is missing, not a regular file, empty, too large or
    # still moving. It is not written back.
    REFRESH_UNSAFE = "REFRESH_UNSAFE"
    # Writing it back failed. The source is still the complete old file.
    WRITEBACK_FAILED = "WRITEBACK_FAILED"


# The outcomes after which the source no longer holds a usable generation, or
# may not: each is reported as a failure of the attempt rather than passed over.
PERSISTENCE_FAILURES: dict[Persistence, str] = {
    Persistence.SOURCE_CHANGED: SOURCE_CHANGED,
    Persistence.REFRESH_UNSAFE: "CODEX_AUTH_REFRESH_UNSAFE",
    Persistence.WRITEBACK_FAILED: "CODEX_AUTH_WRITEBACK_FAILED",
}


@dataclass(frozen=True)
class IsolatedHome:
    """A private CODEX_HOME and whether the login state made it in.

    `source_state` is the source `auth.json` the copy was taken from, and
    `copy_state` the copy as it was handed to the CLI. Both are None when no
    login state was copied.
    """

    path: Path
    auth_present: bool
    source_auth: Path | None = None
    source_state: FileState | None = None
    copy_state: FileState | None = None

    def discard(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def _seed_installation_id(source_home: Path, target: Path) -> None:
    """Put an `installation_id` in place before the CLI looks for one.

    Reuses the source machine's identifier when there is one, so a probe run
    does not look like a fresh installation to the provider. When there is
    none, a new UUID is generated **here** -- the user's own `CODEX_HOME` is
    never written to, not even to create this file.
    """
    destination = target / INSTALLATION_ID_FILE
    source = source_home / INSTALLATION_ID_FILE
    if source.is_file():
        # copyfile, like the auth file: the value never becomes a Python
        # string in this process.
        shutil.copyfile(source, destination)
    else:
        destination.write_text(str(uuid.uuid4()), encoding="utf-8")
    os.chmod(destination, INSTALLATION_ID_MODE)


def _copy_descriptor(source: int, destination: int, limit: int) -> int:
    """Copy bytes between two open files. Never decoded, never kept."""
    copied = 0
    while True:
        chunk = os.read(source, 65536)
        if not chunk:
            return copied
        copied += len(chunk)
        if copied > limit:
            raise AuthSourceError(SOURCE_UNSAFE)
        view = memoryview(chunk)
        while view:
            written = os.write(destination, view)
            view = view[written:]


def _copy_source_auth(source: Path, destination: Path) -> FileState:
    """Copy the source login state through one descriptor; return what was copied.

    Opened without following links, so the state recorded is the state of the
    file that was read, and checked again afterwards, so a source that moved
    during the copy is never mistaken for the generation the copy holds.
    `O_NONBLOCK` because the path is only known to be a regular file until it
    is opened: swapped for a FIFO in between, a blocking open would wait for a
    writer forever instead of refusing.
    """
    try:
        reader = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise AuthSourceError(SOURCE_UNSAFE) from None
    try:
        before = FileState.of(os.fstat(reader))
        if not stat.S_ISREG(before.mode):
            raise AuthSourceError(SOURCE_UNSAFE)
        writer = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, AUTH_MODE
        )
        try:
            copied = _copy_descriptor(reader, writer, MAX_AUTH_BYTES)
        finally:
            os.close(writer)
        after = FileState.of(os.fstat(reader))
    finally:
        os.close(reader)
    if after != before or copied != before.size or file_state(source) != before:
        raise AuthSourceError(SOURCE_CHANGED)
    return before


def build_isolated_home(source_home: Path, parent: Path) -> IsolatedHome | None:
    """Create the private home holding exactly the two expected files.

    Returns None when it cannot be created. A missing source auth file is not
    an error here -- it produces a home with `auth_present=False`, and the
    preflight decides what that means. A source auth file that is a link or not
    a regular file raises `AuthSourceError`: that is not an absent login, it is
    one that cannot be leased safely.
    """
    try:
        target = Path(os.path.join(parent, "codex-home"))
        target.mkdir(mode=HOME_MODE, parents=False, exist_ok=False)
    except OSError:
        return None

    source = source_home / AUTH_FILE
    try:
        present = file_state(source)
    except OSError:
        shutil.rmtree(target, ignore_errors=True)
        return None
    if present is None:
        return IsolatedHome(path=target, auth_present=False)
    if not stat.S_ISREG(present.mode):
        shutil.rmtree(target, ignore_errors=True)
        raise AuthSourceError(SOURCE_UNSAFE)

    destination = target / AUTH_FILE
    try:
        source_state = _copy_source_auth(source, destination)
        os.chmod(destination, AUTH_MODE)
        os.utime(destination, ns=(COPY_SENTINEL_NS, COPY_SENTINEL_NS), follow_symlinks=False)
        copy_state = file_state(destination)
        _seed_installation_id(source_home, target)
    except AuthSourceError:
        shutil.rmtree(target, ignore_errors=True)
        raise
    except OSError:
        shutil.rmtree(target, ignore_errors=True)
        return None

    return IsolatedHome(
        path=target,
        auth_present=True,
        source_auth=source,
        source_state=source_state,
        copy_state=copy_state,
    )


def refreshed_in_isolation(home: IsolatedHome) -> bool:
    """Whether the CLI rewrote the copy. File properties only, never content."""
    if not home.auth_present or home.copy_state is None:
        return False
    try:
        return file_state(home.path / AUTH_FILE) != home.copy_state
    except OSError:
        return True


def _fsync_directory(directory: Path) -> None:
    """Make the rename durable where the platform allows a directory fsync."""
    try:
        descriptor = os.open(directory, os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _source_unchanged(home: IsolatedHome) -> bool:
    assert home.source_auth is not None
    try:
        return file_state(home.source_auth) == home.source_state
    except OSError:
        return False


def persist_refresh(home: IsolatedHome) -> Persistence:
    """Bring a refreshed login state back into the source, or say why not.

    Only `auth.json`, only when the copy was rewritten, only while the source
    is the file the copy was taken from, and only as a whole file. Never raises:
    every failure is an outcome, and none of them carries a path or a byte.
    """
    if not home.auth_present or home.source_auth is None or home.source_state is None:
        return Persistence.NOT_APPLICABLE
    if not refreshed_in_isolation(home):
        return Persistence.UNCHANGED

    copy = home.path / AUTH_FILE
    try:
        current = file_state(copy)
    except OSError:
        return Persistence.REFRESH_UNSAFE
    if (
        current is None
        or not stat.S_ISREG(current.mode)
        or current.size == 0
        or current.size > MAX_AUTH_BYTES
    ):
        return Persistence.REFRESH_UNSAFE
    if not _source_unchanged(home):
        return Persistence.SOURCE_CHANGED

    directory = home.source_auth.parent
    try:
        writer, temporary_name = tempfile.mkstemp(
            dir=directory, prefix=WRITEBACK_PREFIX, suffix=WRITEBACK_SUFFIX
        )
    except OSError:
        return Persistence.WRITEBACK_FAILED
    temporary = Path(temporary_name)
    replaced = False
    try:
        try:
            os.fchmod(writer, AUTH_MODE)
            reader = os.open(copy, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                opened = FileState.of(os.fstat(reader))
                if opened != current:
                    return Persistence.REFRESH_UNSAFE
                try:
                    copied = _copy_descriptor(reader, writer, MAX_AUTH_BYTES)
                except AuthSourceError:
                    return Persistence.REFRESH_UNSAFE
                if copied != current.size or FileState.of(os.fstat(reader)) != current:
                    # Still being written, or not the file that was judged.
                    return Persistence.REFRESH_UNSAFE
            finally:
                os.close(reader)
            os.fsync(writer)
        finally:
            os.close(writer)
        if not _source_unchanged(home):
            return Persistence.SOURCE_CHANGED
        os.replace(temporary, home.source_auth)
        replaced = True
    except OSError:
        return Persistence.WRITEBACK_FAILED
    finally:
        if not replaced:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    _fsync_directory(directory)
    return Persistence.PERSISTED


__all__ = [
    "AUTH_FILE",
    "AUTH_MODE",
    "COPY_SENTINEL_NS",
    "EXPECTED_ENTRIES",
    "HOME_MODE",
    "INSTALLATION_ID_FILE",
    "INSTALLATION_ID_MODE",
    "MAX_AUTH_BYTES",
    "PERSISTENCE_FAILURES",
    "SOURCE_CHANGED",
    "SOURCE_UNSAFE",
    "AuthSourceError",
    "FileState",
    "IsolatedHome",
    "Persistence",
    "build_isolated_home",
    "file_state",
    "persist_refresh",
    "refreshed_in_isolation",
]
