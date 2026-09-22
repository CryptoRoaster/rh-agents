"""The catalog guard, including what the real 0.153.4 catalog actually says."""

import json

from src.evaluation.codex.catalog import read_tool_surface, unsupported_reason

# Copied from `codex debug models --bundled` of the supported build. The fields
# are the ones `core/src/tools/spec_plan.rs` reads when it builds the tool plan.
REAL_SOL_ENTRY = {
    "slug": "gpt-5.6-sol",
    "tool_mode": "code_mode_only",
    "shell_type": "unified_exec",
    "apply_patch_tool_type": "freeform",
    "experimental_supported_tools": [],
}
REAL_LEGACY_ENTRY = {
    "slug": "gpt-5.4",
    "tool_mode": None,
    "shell_type": "unified_exec",
    "apply_patch_tool_type": "freeform",
    "experimental_supported_tools": [],
}


def catalog(*entries: dict[str, object]) -> str:
    return json.dumps({"models": list(entries)})


def test_the_configured_frontier_model_is_refused() -> None:
    """The finding, pinned as a test.

    `gpt-5.6-sol` declares `tool_mode = "code_mode_only"`, and
    `requested_tool_mode` reads the catalog before it reads any feature flag, so
    `--disable code_mode` does not remove code mode from this model. Code mode
    executes model-written code locally, which reads files.
    """
    surface = read_tool_surface(catalog(REAL_SOL_ENTRY), "gpt-5.6-sol")
    assert surface is not None
    assert surface.tool_mode == "code_mode_only"
    assert unsupported_reason(surface) == "TOOL_MODE_CODE_MODE_ONLY"


def test_a_model_without_a_declared_tool_mode_is_accepted() -> None:
    surface = read_tool_surface(catalog(REAL_LEGACY_ENTRY), "gpt-5.4")
    assert surface is not None
    assert surface.tool_mode is None
    assert unsupported_reason(surface) is None


def test_an_experimental_tool_is_refused() -> None:
    entry = dict(REAL_LEGACY_ENTRY, experimental_supported_tools=["clock"])
    surface = read_tool_surface(catalog(entry), "gpt-5.4")
    assert surface is not None
    assert unsupported_reason(surface) == "EXPERIMENTAL_TOOL_CLOCK"


def test_an_unknown_apply_patch_type_is_refused() -> None:
    entry = dict(REAL_LEGACY_ENTRY, apply_patch_tool_type="something_new")
    surface = read_tool_surface(catalog(entry), "gpt-5.4")
    assert surface is not None
    assert unsupported_reason(surface) == "APPLY_PATCH_SOMETHING_NEW"


def test_apply_patch_is_recorded_even_when_accepted() -> None:
    """It is always offered, so the guard records it rather than pretending."""
    surface = read_tool_surface(catalog(REAL_LEGACY_ENTRY), "gpt-5.4")
    assert surface is not None
    assert surface.apply_patch_tool_type == "freeform"


def test_a_missing_model_yields_no_surface() -> None:
    assert read_tool_surface(catalog(REAL_SOL_ENTRY), "gpt-5.4") is None


def test_an_unreadable_catalog_yields_no_surface() -> None:
    assert read_tool_surface("not json", "gpt-5.4") is None
    assert read_tool_surface(json.dumps({"models": "nope"}), "gpt-5.4") is None
    assert read_tool_surface(json.dumps(["nope"]), "gpt-5.4") is None


def test_a_malformed_experimental_list_is_refused() -> None:
    entry = dict(REAL_LEGACY_ENTRY, experimental_supported_tools="clock")
    surface = read_tool_surface(catalog(entry), "gpt-5.4")
    assert surface is not None
    assert unsupported_reason(surface) == "EXPERIMENTAL_TOOL__MALFORMED_"
