"""The isolated CODEX_HOME: two files, restrictive modes, nothing read.

Two, not one. `codex exec` starts an in-process app-server client whose
`resolve_installation_id` opens `CODEX_HOME/installation_id`, and the
second authorised real probe died on its absence. The file is seeded here
rather than left for the CLI to create, because letting the CLI create it
would mean a writable directory -- and a writable directory means an
unknown file set and an isolation gate with nothing left to check.
"""

import uuid
from pathlib import Path

from src.evaluation.codex.auth_home import (
    AUTH_FILE,
    EXPECTED_ENTRIES,
    INSTALLATION_ID_FILE,
    build_isolated_home,
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


def test_the_user_home_is_never_written_to(tmp_path: Path) -> None:
    """A refresh during an attempt lands in the copy, not in the real home."""
    source = tmp_path / "user-home"
    source.mkdir()
    (source / AUTH_FILE).write_text(PLACEHOLDER, encoding="utf-8")
    before = sorted(item.name for item in source.iterdir())

    home = build_isolated_home(source, tmp_path)
    assert home is not None
    try:
        (home.path / AUTH_FILE).write_text('{"placeholder": "refreshed"}', encoding="utf-8")
        assert (source / AUTH_FILE).read_text(encoding="utf-8") == PLACEHOLDER
    finally:
        home.discard()
    assert sorted(item.name for item in source.iterdir()) == before


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
