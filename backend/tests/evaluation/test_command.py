"""What we pass to the CLI, and what the child is allowed to inherit.

These tests bound our own argument construction. They do not show what the CLI
then offers the model: a later build could enable a tool by default or ignore an
override and every assertion here would still pass. The version pin is what
guards that, not this file.
"""

from pathlib import Path

import pytest

from src.evaluation.codex.command import (
    ALLOWED_ENVIRONMENT_KEYS,
    BASE_CONFIG_OVERRIDES,
    DISABLED_FEATURES,
    FORBIDDEN_ARGUMENTS,
    CommandBuildError,
    build_arguments,
    build_preflight_arguments,
    child_environment,
    toml_string,
)
from src.evaluation.codex.models import CodexLauncher, EvaluationFailure, LauncherKind

BINARY = CodexLauncher(kind=LauncherKind.PLATFORM_BINARY, executable=Path("/opt/codex"))


def arguments(tmp_path: Path, **overrides: object) -> list[str]:
    settings: dict[str, object] = {
        "launcher": BINARY,
        "working_directory": tmp_path / "workspace",
        "schema_path": tmp_path / "schema.json",
        "model": "gpt-5.6-sol",
        "effort": "low",
        "instructions": "Answer as JSON only.",
        "model_catalog_path": tmp_path / "model-catalog.json",
    }
    settings.update(overrides)
    (tmp_path / "workspace").mkdir(exist_ok=True)
    return build_arguments(**settings)  # type: ignore[arg-type]


def test_every_lock_switch_is_present(tmp_path: Path) -> None:
    built = arguments(tmp_path)
    assert built[1:3] == ["exec", "--json"]
    for flag in ("--ignore-user-config", "--ephemeral", "--skip-git-repo-check"):
        assert flag in built
    assert built[built.index("--sandbox") + 1] == "read-only"
    for feature in DISABLED_FEATURES:
        assert ["--disable", feature] == built[built.index(feature) - 1 : built.index(feature) + 1]
    for override in BASE_CONFIG_OVERRIDES:
        assert override in built
    # The prompt is read from stdin, so untrusted data never reaches argv.
    assert built[-1] == "-"


def test_no_forbidden_flag_is_ever_emitted(tmp_path: Path) -> None:
    built = arguments(tmp_path)
    for flag in FORBIDDEN_ARGUMENTS:
        assert flag not in built


def test_instructions_travel_as_a_quoted_toml_string(tmp_path: Path) -> None:
    built = arguments(tmp_path, instructions='Say "no" \\ then stop.\nSecond line.')
    override = next(item for item in built if item.startswith("developer_instructions="))
    value = override.removeprefix("developer_instructions=")
    assert value == toml_string('Say "no" \\ then stop.\nSecond line.')
    assert value.startswith('"') and value.endswith('"')


def test_effort_is_passed_verbatim_or_not_at_all(tmp_path: Path) -> None:
    assert 'model_reasoning_effort="low"' in arguments(tmp_path)
    assert not any(
        item.startswith("model_reasoning_effort") for item in arguments(tmp_path, effort=None)
    )


def test_workspace_inside_a_forbidden_root_is_refused(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    (repository / "workspace").mkdir(parents=True)
    with pytest.raises(CommandBuildError) as caught:
        build_arguments(
            launcher=BINARY,
            working_directory=repository / "workspace",
            schema_path=tmp_path / "schema.json",
            model="gpt-5.6-sol",
            effort=None,
            instructions="x",
            model_catalog_path=tmp_path / "model-catalog.json",
            forbidden_roots=(repository,),
        )
    assert caught.value.reason_code == "WORKSPACE_INSIDE_FORBIDDEN_ROOT"


def test_node_shim_without_node_on_child_path_is_refused(tmp_path: Path) -> None:
    shim = CodexLauncher(
        kind=LauncherKind.NODE_SHIM,
        executable=tmp_path / "codex.js",
        path_entries=(tmp_path / "empty",),
    )
    (tmp_path / "empty").mkdir()
    with pytest.raises(CommandBuildError) as caught:
        build_preflight_arguments(launcher=shim)
    assert caught.value.failure is EvaluationFailure.LAUNCHER_UNSUPPORTED
    assert caught.value.reason_code == "NODE_NOT_ON_CHILD_PATH"


def test_node_shim_with_node_on_child_path_is_accepted(tmp_path: Path) -> None:
    node_dir = tmp_path / "bin"
    node_dir.mkdir()
    (node_dir / "node").write_text("#!/bin/sh\n", encoding="utf-8")
    shim = CodexLauncher(
        kind=LauncherKind.NODE_SHIM,
        executable=tmp_path / "codex.js",
        path_entries=(node_dir,),
    )
    assert build_preflight_arguments(launcher=shim)[1:] == ["login", "status"]


def test_child_environment_holds_only_the_allowed_keys(tmp_path: Path) -> None:
    built = child_environment(
        codex_home=tmp_path / "codex",
        home=tmp_path / "home",
        tmpdir=tmp_path / "tmp",
        path_entries=(Path("/usr/bin"),),
    )
    assert set(built) == set(ALLOWED_ENVIRONMENT_KEYS)
    for banned in (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "CODEX_REFRESH_TOKEN_URL_OVERRIDE",
        "CODEX_REVOKE_TOKEN_URL_OVERRIDE",
        "CODEX_APP_SERVER_LOGIN_CLIENT_ID",
        "ANTHROPIC_API_KEY",
        "DATABASE_URL",
    ):
        assert banned not in built


def test_the_model_catalog_is_always_pinned(tmp_path: Path) -> None:
    """Without the pin, `exec` would resolve ModelInfo through the ModelsManager.

    That path is `RefreshStrategy::OnlineIfUncached`: a fresh cache entry or a
    remote `/models` response could carry a different `tool_mode` than the file
    the harness judged. `model_catalog_json` makes the provider build a
    `StaticModelsManager` instead, which never refreshes and never reads the
    cache.
    """
    catalog = tmp_path / "model-catalog.json"
    built = arguments(tmp_path, model_catalog_path=catalog)
    assert f"model_catalog_json={toml_string(str(catalog))}" in built


def test_a_relative_catalog_path_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CommandBuildError) as caught:
        arguments(tmp_path, model_catalog_path=Path("model-catalog.json"))
    assert caught.value.reason_code == "CATALOG_PATH_NOT_ABSOLUTE"
