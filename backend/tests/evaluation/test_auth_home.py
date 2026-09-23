"""The isolated CODEX_HOME: one file, restrictive modes, nothing read."""

from pathlib import Path

from src.evaluation.codex.auth_home import AUTH_FILE, build_isolated_home

# Deliberately not a token shape. Nothing here resembles a credential, and no
# test in this file asserts on the content of a real one.
PLACEHOLDER = '{"placeholder": "not a credential"}'


def test_only_the_auth_file_is_carried_over(tmp_path: Path) -> None:
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
        assert sorted(item.name for item in home.path.iterdir()) == [AUTH_FILE]
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
