"""The catalog guard, including what the real 0.153.4 catalog actually says."""

import json
from pathlib import Path

from src.evaluation.codex.catalog import judge_catalog, judge_catalog_file

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


def test_the_frontier_model_entry_is_refused() -> None:
    """The finding, pinned as a test.

    `gpt-5.6-sol` declares `tool_mode = "code_mode_only"`, and
    `requested_tool_mode` reads the catalog before it reads any feature flag, so
    `--disable code_mode` does not remove code mode from this model. Code mode
    executes model-written code locally, which reads files.
    """
    verdict = judge_catalog(catalog(REAL_SOL_ENTRY), "gpt-5.6-sol")
    assert verdict.surface is not None
    assert verdict.surface.tool_mode == "code_mode_only"
    assert verdict.reason == "TOOL_MODE_CODE_MODE_ONLY"


def test_a_model_without_a_declared_tool_mode_is_accepted() -> None:
    verdict = judge_catalog(catalog(REAL_LEGACY_ENTRY), "gpt-5.4")
    assert verdict.reason is None
    assert verdict.surface is not None
    assert verdict.surface.tool_mode is None
    # apply_patch is always offered, so the guard records it rather than
    # pretending it is absent.
    assert verdict.surface.apply_patch_tool_type == "freeform"


def test_a_missing_tool_mode_key_reads_as_no_opinion() -> None:
    entry = {k: v for k, v in REAL_LEGACY_ENTRY.items() if k != "tool_mode"}
    assert judge_catalog(catalog(entry), "gpt-5.4").reason is None


def test_an_experimental_tool_is_refused() -> None:
    entry = dict(REAL_LEGACY_ENTRY, experimental_supported_tools=["clock"])
    assert judge_catalog(catalog(entry), "gpt-5.4").reason == "EXPERIMENTAL_TOOL_CLOCK"


def test_an_unknown_apply_patch_type_is_refused() -> None:
    entry = dict(REAL_LEGACY_ENTRY, apply_patch_tool_type="something_new")
    assert judge_catalog(catalog(entry), "gpt-5.4").reason == "APPLY_PATCH_SOMETHING_NEW"


def test_a_wrong_type_is_refused_and_never_read_as_null() -> None:
    """`{"unexpected": "shape"}` is not `null`, and must not be treated as one.

    Coercing an unexpected shape to "absent" turns a parsing accident into a
    permission, which is exactly backwards for a guard.
    """
    for bad in ({"unexpected": "shape"}, 7, [], True):
        entry = dict(REAL_LEGACY_ENTRY, tool_mode=bad)
        assert judge_catalog(catalog(entry), "gpt-5.4").reason == "TOOL_MODE_MALFORMED"
    for bad in (7, {"a": 1}, []):
        entry = dict(REAL_LEGACY_ENTRY, apply_patch_tool_type=bad)
        assert judge_catalog(catalog(entry), "gpt-5.4").reason == "APPLY_PATCH_MALFORMED"


def test_a_malformed_or_missing_experimental_list_is_refused() -> None:
    for bad in ("clock", 7, {"a": 1}, [1, 2], [None]):
        entry = dict(REAL_LEGACY_ENTRY, experimental_supported_tools=bad)
        assert judge_catalog(catalog(entry), "gpt-5.4").reason == "EXPERIMENTAL_TOOLS_MALFORMED"
    entry = {k: v for k, v in REAL_LEGACY_ENTRY.items() if k != "experimental_supported_tools"}
    assert judge_catalog(catalog(entry), "gpt-5.4").reason == "EXPERIMENTAL_TOOLS_MALFORMED"


def test_a_missing_model_is_refused() -> None:
    assert judge_catalog(catalog(REAL_SOL_ENTRY), "gpt-5.4").reason == "MODEL_NOT_IN_CATALOG"


def test_an_unreadable_catalog_is_refused() -> None:
    assert judge_catalog("not json", "gpt-5.4").reason == "CATALOG_NOT_JSON"
    assert judge_catalog(json.dumps(["nope"]), "gpt-5.4").reason == "CATALOG_NOT_OBJECT"
    assert (
        judge_catalog(json.dumps({"models": "nope"}), "gpt-5.4").reason == "CATALOG_HAS_NO_MODELS"
    )


def test_a_missing_catalog_file_is_refused(tmp_path: Path) -> None:
    verdict = judge_catalog_file(tmp_path / "absent.json", "gpt-5.4")
    assert verdict.surface is None
    assert verdict.reason == "CATALOG_UNREADABLE"
