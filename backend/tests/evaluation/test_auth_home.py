"""The isolated CODEX_HOME: two files, restrictive modes, nothing parsed.

And a refreshed login state comes back. Codex rotates refresh tokens, so a
copy the CLI rewrote holds the only generation the provider still accepts; it
is written back into the source `auth.json` -- that one file, atomically, and
only while the source is still the file the copy was taken from.

Two, not one. `codex exec` starts an in-process app-server client whose
`resolve_installation_id` opens `CODEX_HOME/installation_id`, and the
second authorised real probe died on its absence. The file is seeded here
rather than left for the CLI to create, because letting the CLI create it
would mean a writable directory -- and a writable directory means an
unknown file set and an isolation gate with nothing left to check.
"""

import os
import tempfile
import uuid
from pathlib import Path

import pytest

from src.evaluation.codex.auth_home import (
    AUTH_FILE,
    COPY_SENTINEL_NS,
    EXPECTED_ENTRIES,
    INSTALLATION_ID_FILE,
    SOURCE_CHANGED,
    SOURCE_UNSAFE,
    AuthSourceError,
    IsolatedHome,
    Persistence,
    build_isolated_home,
    file_state,
    persist_refresh,
    refreshed_in_isolation,
)

# Deliberately not a token shape. Nothing here resembles a credential, and no
# test in this file asserts on the content of a real one.
PLACEHOLDER = '{"placeholder": "not a credential"}'


def test_only_the_two_expected_files_are_carried_over(tmp_path: Path) -> None:
    source = tmp_path / "user-home"
    source.mkdir()
    (source / AUTH_FILE).write_text(PLACEHOLDER, encoding="utf-8")
    for noise in ("config.toml", "history.jsonl", "models_cache.json"):
        (source / noise).write_text("should not travel", encoding="utf-8")
    (source / "sessions").mkdir()

    home = build_isolated_home(source, tmp_path)
    assert home is not None
    try:
        assert home.auth_present is True
        assert sorted(item.name for item in home.path.iterdir()) == sorted(EXPECTED_ENTRIES)
    finally:
        home.discard()
    assert not home.path.exists()


def test_the_modes_are_restrictive(tmp_path: Path) -> None:
    source = tmp_path / "user-home"
    source.mkdir()
    (source / AUTH_FILE).write_text(PLACEHOLDER, encoding="utf-8")
    (source / AUTH_FILE).chmod(0o644)

    home = build_isolated_home(source, tmp_path)
    assert home is not None
    try:
        assert home.path.stat().st_mode & 0o777 == 0o700
        # Copied rather than cloned: a permissive source mode does not travel.
        assert (home.path / AUTH_FILE).stat().st_mode & 0o777 == 0o600
        # 0644 is the mode `resolve_installation_id` checks for and repairs,
        # so seeding it correctly makes that repair a no-op rather than a
        # dependency on one more permitted operation.
        assert (home.path / INSTALLATION_ID_FILE).stat().st_mode & 0o777 == 0o644
    finally:
        home.discard()


def test_a_missing_login_state_is_reported_not_invented(tmp_path: Path) -> None:
    source = tmp_path / "user-home"
    source.mkdir()
    home = build_isolated_home(source, tmp_path)
    assert home is not None
    try:
        assert home.auth_present is False
        assert list(home.path.iterdir()) == []
    finally:
        home.discard()


def test_an_existing_installation_id_is_reused(tmp_path: Path) -> None:
    """The source machine's identifier travels, so a probe is not a new install.

    The value is compared byte for byte without being looked at: it is not a
    credential, but it is a persistent identifier and has no business in a log,
    an assertion message or a report.
    """
    source = tmp_path / "user-home"
    source.mkdir()
    (source / AUTH_FILE).write_text(PLACEHOLDER, encoding="utf-8")
    (source / INSTALLATION_ID_FILE).write_text(str(uuid.uuid4()), encoding="utf-8")
    expected = (source / INSTALLATION_ID_FILE).read_bytes()

    home = build_isolated_home(source, tmp_path)
    assert home is not None
    try:
        assert (home.path / INSTALLATION_ID_FILE).read_bytes() == expected
    finally:
        home.discard()


def test_a_missing_installation_id_is_generated_in_the_copy_only(tmp_path: Path) -> None:
    """Never written into the user's own home, not even to create this file."""
    source = tmp_path / "user-home"
    source.mkdir()
    (source / AUTH_FILE).write_text(PLACEHOLDER, encoding="utf-8")

    home = build_isolated_home(source, tmp_path)
    assert home is not None
    try:
        generated = (home.path / INSTALLATION_ID_FILE).read_text(encoding="utf-8")
        # A well-formed UUID, because that is what the CLI accepts as already
        # resolved; anything else would make it rewrite the file on startup.
        assert uuid.UUID(generated)
        assert not (source / INSTALLATION_ID_FILE).exists()
        assert sorted(item.name for item in source.iterdir()) == [AUTH_FILE]
    finally:
        home.discard()


# --------------------------------------------------------------- write-back
#
# Every credential here is a dummy. No real `auth.json` is read in this file.

GENERATION_1 = b'{"placeholder": "generation-1"}'
GENERATION_2 = b'{"placeholder": "generation-2", "longer": true}'
# Same length as GENERATION_1, different bytes: a rewrite the size cannot show.
GENERATION_1_SAME_SIZE = b'{"placeholder": "generation-X"}'
assert len(GENERATION_1_SAME_SIZE) == len(GENERATION_1)

OTHER_FILES = {
    "config.toml": b"model = 'placeholder'\n",
    INSTALLATION_ID_FILE: b"00000000-0000-4000-8000-000000000001",
    "history.jsonl": b'{"placeholder": "history"}\n',
}


def user_home(tmp_path: Path, content: bytes = GENERATION_1) -> Path:
    source = tmp_path / "user-home"
    source.mkdir()
    (source / AUTH_FILE).write_bytes(content)
    (source / AUTH_FILE).chmod(0o600)
    for name, data in OTHER_FILES.items():
        (source / name).write_bytes(data)
    (source / "sessions").mkdir()
    (source / "sessions" / "one.jsonl").write_bytes(b"{}\n")
    return source


def fingerprint(source: Path) -> dict[str, tuple[bytes, int, int]]:
    """Every file under the source home except `auth.json`: bytes, mode, mtime."""
    state: dict[str, tuple[bytes, int, int]] = {}
    for path in sorted(source.rglob("*")):
        if path.is_file() and path.name != AUTH_FILE:
            info = path.stat()
            state[str(path.relative_to(source))] = (
                path.read_bytes(),
                info.st_mode,
                info.st_mtime_ns,
            )
    return state


def isolated(source: Path, tmp_path: Path) -> IsolatedHome:
    parent = tmp_path / "attempt"
    parent.mkdir()
    home = build_isolated_home(source, parent)
    assert home is not None
    return home


def refresh(home: IsolatedHome, content: bytes) -> None:
    """What Codex 0.153.4 does: truncate, write, in place."""
    with open(home.path / AUTH_FILE, "r+b") as handle:
        handle.truncate(0)
        handle.write(content)


def test_the_copy_is_pinned_so_that_any_rewrite_shows(tmp_path: Path) -> None:
    source = user_home(tmp_path)
    home = isolated(source, tmp_path)
    copy = home.path / AUTH_FILE
    assert copy.stat().st_mtime_ns == COPY_SENTINEL_NS
    assert home.copy_state == file_state(copy)
    assert home.source_state == file_state(source / AUTH_FILE)
    assert refreshed_in_isolation(home) is False


def test_without_a_refresh_the_source_is_untouched(tmp_path: Path) -> None:
    source = user_home(tmp_path)
    auth_before = file_state(source / AUTH_FILE)
    others_before = fingerprint(source)
    listing_before = sorted(item.name for item in source.iterdir())

    home = isolated(source, tmp_path)
    assert persist_refresh(home) is Persistence.UNCHANGED
    home.discard()

    assert file_state(source / AUTH_FILE) == auth_before
    assert (source / AUTH_FILE).read_bytes() == GENERATION_1
    assert fingerprint(source) == others_before
    assert sorted(item.name for item in source.iterdir()) == listing_before


@pytest.mark.parametrize("content", [GENERATION_2, GENERATION_1_SAME_SIZE])
def test_a_refresh_comes_back_and_only_auth_json_changes(tmp_path: Path, content: bytes) -> None:
    source = user_home(tmp_path)
    others_before = fingerprint(source)
    listing_before = sorted(item.name for item in source.iterdir())

    home = isolated(source, tmp_path)
    refresh(home, content)
    assert refreshed_in_isolation(home) is True
    assert persist_refresh(home) is Persistence.PERSISTED
    home.discard()

    assert (source / AUTH_FILE).read_bytes() == content
    assert (source / AUTH_FILE).stat().st_mode & 0o777 == 0o600
    # config.toml, installation_id, history and sessions: not one byte, mode
    # or timestamp moved, and no write-back file is left beside them.
    assert fingerprint(source) == others_before
    assert sorted(item.name for item in source.iterdir()) == listing_before


def test_a_source_replaced_meanwhile_wins(tmp_path: Path) -> None:
    """A manual login during the attempt is not overwritten by the attempt."""
    source = user_home(tmp_path)
    home = isolated(source, tmp_path)
    refresh(home, GENERATION_2)
    external = source / "login.tmp"
    external.write_bytes(b'{"placeholder": "signed in by hand"}')
    os.replace(external, source / AUTH_FILE)

    assert persist_refresh(home) is Persistence.SOURCE_CHANGED
    assert (source / AUTH_FILE).read_bytes() == b'{"placeholder": "signed in by hand"}'


def test_a_source_rewritten_in_place_meanwhile_wins(tmp_path: Path) -> None:
    source = user_home(tmp_path)
    home = isolated(source, tmp_path)
    refresh(home, GENERATION_2)
    (source / AUTH_FILE).write_bytes(GENERATION_1_SAME_SIZE)

    assert persist_refresh(home) is Persistence.SOURCE_CHANGED
    assert (source / AUTH_FILE).read_bytes() == GENERATION_1_SAME_SIZE


def test_a_logout_meanwhile_is_not_undone(tmp_path: Path) -> None:
    source = user_home(tmp_path)
    home = isolated(source, tmp_path)
    refresh(home, GENERATION_2)
    (source / AUTH_FILE).unlink()

    assert persist_refresh(home) is Persistence.SOURCE_CHANGED
    assert not (source / AUTH_FILE).exists()


def test_a_source_that_changes_just_before_the_replace_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source is checked again after the new file is durable, right before the move."""
    source = user_home(tmp_path)
    home = isolated(source, tmp_path)
    refresh(home, GENERATION_2)
    real_fsync = os.fsync

    def login_during_fsync(descriptor: int) -> None:
        real_fsync(descriptor)
        (source / AUTH_FILE).write_bytes(b'{"placeholder": "late login"}')

    with monkeypatch.context() as patched:
        patched.setattr(os, "fsync", login_during_fsync)
        assert persist_refresh(home) is Persistence.SOURCE_CHANGED
    assert (source / AUTH_FILE).read_bytes() == b'{"placeholder": "late login"}'
    assert leftovers(source) == []


def leftovers(source: Path) -> list[str]:
    return [item.name for item in source.iterdir() if item.name.startswith(".auth.json.")]


@pytest.mark.parametrize("step", ["mkstemp", "fsync", "replace", "fchmod"])
def test_a_failed_write_back_leaves_the_complete_old_file_and_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    source = user_home(tmp_path)
    home = isolated(source, tmp_path)
    refresh(home, GENERATION_2)

    def broken(*_args: object, **_kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patched:
        if step == "mkstemp":
            patched.setattr(tempfile, "mkstemp", broken)
        else:
            patched.setattr(os, step, broken)
        outcome = persist_refresh(home)

    assert outcome is Persistence.WRITEBACK_FAILED
    assert (source / AUTH_FILE).read_bytes() == GENERATION_1
    assert leftovers(source) == []


@pytest.mark.parametrize("damage", ["empty", "removed", "symlink"])
def test_an_unusable_rewritten_copy_is_never_written_back(tmp_path: Path, damage: str) -> None:
    source = user_home(tmp_path)
    home = isolated(source, tmp_path)
    copy = home.path / AUTH_FILE
    if damage == "empty":
        refresh(home, b"")
    elif damage == "removed":
        copy.unlink()
    else:
        decoy = tmp_path / "decoy.json"
        decoy.write_bytes(GENERATION_2)
        copy.unlink()
        copy.symlink_to(decoy)

    assert persist_refresh(home) is Persistence.REFRESH_UNSAFE
    assert (source / AUTH_FILE).read_bytes() == GENERATION_1
    assert leftovers(source) == []


def test_a_copy_still_being_written_is_not_written_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy must hold still while it is read; a moving file is not a generation."""
    source = user_home(tmp_path)
    home = isolated(source, tmp_path)
    refresh(home, GENERATION_2)
    real_read = os.read
    copy = home.path / AUTH_FILE

    moved: list[bool] = []

    def read_while_codex_writes(descriptor: int, size: int) -> bytes:
        chunk = real_read(descriptor, size)
        if chunk and not moved:
            moved.append(True)
            with open(copy, "ab") as handle:
                handle.write(b" ")
        return chunk

    with monkeypatch.context() as patched:
        patched.setattr(os, "read", read_while_codex_writes)
        outcome = persist_refresh(home)
    assert outcome is Persistence.REFRESH_UNSAFE
    assert (source / AUTH_FILE).read_bytes() == GENERATION_1
    assert leftovers(source) == []


def test_no_login_state_means_nothing_to_bring_back(tmp_path: Path) -> None:
    source = tmp_path / "user-home"
    source.mkdir()
    home = isolated(source, tmp_path)
    assert persist_refresh(home) is Persistence.NOT_APPLICABLE
    assert list(source.iterdir()) == []


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_a_source_that_is_not_a_regular_file_refuses(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "user-home"
    source.mkdir()
    target = source / AUTH_FILE
    if kind == "symlink":
        real = tmp_path / "elsewhere.json"
        real.write_bytes(GENERATION_1)
        target.symlink_to(real)
    elif kind == "directory":
        target.mkdir()
    else:
        os.mkfifo(target)
    parent = tmp_path / "attempt"
    parent.mkdir()

    with pytest.raises(AuthSourceError) as caught:
        build_isolated_home(source, parent)
    assert caught.value.code == SOURCE_UNSAFE
    assert str(caught.value) == SOURCE_UNSAFE
    # Nothing half-built is left behind.
    assert list(parent.iterdir()) == []


def test_a_source_that_moves_during_the_copy_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = user_home(tmp_path)
    parent = tmp_path / "attempt"
    parent.mkdir()
    real_read = os.read

    moved: list[bool] = []

    def login_during_copy(descriptor: int, size: int) -> bytes:
        chunk = real_read(descriptor, size)
        if chunk and not moved:
            moved.append(True)
            (source / AUTH_FILE).write_bytes(GENERATION_2)
        return chunk

    with monkeypatch.context() as patched, pytest.raises(AuthSourceError) as caught:
        patched.setattr(os, "read", login_during_copy)
        build_isolated_home(source, parent)
    assert caught.value.code == SOURCE_CHANGED
    assert list(parent.iterdir()) == []


def test_no_outcome_or_error_carries_a_path_or_a_byte(tmp_path: Path) -> None:
    source = user_home(tmp_path)
    home = isolated(source, tmp_path)
    refresh(home, GENERATION_2)
    (source / AUTH_FILE).write_bytes(GENERATION_1_SAME_SIZE)
    outcome = persist_refresh(home)
    for text in (str(outcome), repr(outcome), str(AuthSourceError(SOURCE_UNSAFE))):
        assert "placeholder" not in text
        assert str(tmp_path) not in text


def test_a_source_swapped_for_a_fifo_after_the_check_refuses_instead_of_hanging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between the `lstat` and the open, the path is only believed to be a file."""
    from src.evaluation.codex import auth_home

    source = tmp_path / "user-home"
    source.mkdir()
    (source / AUTH_FILE).write_bytes(GENERATION_1)
    believed = file_state(source / AUTH_FILE)
    (source / AUTH_FILE).unlink()
    os.mkfifo(source / AUTH_FILE)
    real_state = auth_home.file_state

    def stale_check(path: Path) -> object:
        return believed if path == source / AUTH_FILE else real_state(path)

    monkeypatch.setattr(auth_home, "file_state", stale_check)
    parent = tmp_path / "attempt"
    parent.mkdir()
    with pytest.raises(AuthSourceError) as caught:
        build_isolated_home(source, parent)
    assert caught.value.code == SOURCE_UNSAFE
    assert list(parent.iterdir()) == []
