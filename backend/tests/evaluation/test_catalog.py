"""The catalog guard, including what the real 0.153.4 catalog actually says."""

import json
from pathlib import Path

from src.evaluation.codex.catalog import (
    judge_catalog,
    judge_open_catalog,
    open_catalog,
)

# Copied from `codex debug models --bundled` of the supported build. The fields
# are the ones `core/src/tools/spec_plan.rs` reads when it builds the tool plan.
REAL_SOL_ENTRY = {
    "slug": "gpt-5.6-sol",
    "tool_mode": "code_mode_only",
    "shell_type": "unified_exec",
    "apply_patch_tool_type": "freeform",
    "experimental_supported_tools": [],
    "use_responses_lite": True,
}
REAL_LEGACY_ENTRY = {
    "slug": "gpt-5.4",
    "tool_mode": None,
    "shell_type": "unified_exec",
    "apply_patch_tool_type": "freeform",
    "experimental_supported_tools": [],
    "use_responses_lite": False,
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


def test_a_missing_catalog_file_cannot_be_opened(tmp_path: Path) -> None:
    assert open_catalog(tmp_path / "absent.json") is None


def test_the_judged_bytes_survive_the_path_being_replaced(tmp_path: Path) -> None:
    """The binding is to content, not to a name.

    A guard that re-read the path before the run would have a window between the
    two reads. Holding the descriptor closes it: the bytes stay reachable
    through `/dev/fd/<n>` no matter what the directory entry becomes.
    """
    path = tmp_path / "catalog.json"
    path.write_text(catalog(REAL_LEGACY_ENTRY), encoding="utf-8")
    opened = open_catalog(path)
    assert opened is not None
    try:
        swap = tmp_path / "swap.json"
        swap.write_text(
            catalog(dict(REAL_LEGACY_ENTRY, tool_mode="code_mode_only")), encoding="utf-8"
        )
        swap.replace(path)

        assert "code_mode_only" in path.read_text(encoding="utf-8")
        assert "code_mode_only" not in opened.payload
        assert judge_open_catalog(opened, "gpt-5.4", None).reason is None
        assert Path(opened.reference).read_text(encoding="utf-8") == opened.payload
    finally:
        opened.close()


def test_a_digest_pin_refuses_content_that_was_not_reviewed(tmp_path: Path) -> None:
    """The whole approved snapshot is the contract, not the three fields read."""
    path = tmp_path / "catalog.json"
    path.write_text(catalog(REAL_LEGACY_ENTRY), encoding="utf-8")
    opened = open_catalog(path)
    assert opened is not None
    try:
        assert judge_open_catalog(opened, "gpt-5.4", opened.digest).reason is None
        assert judge_open_catalog(opened, "gpt-5.4", "0" * 64).reason == "CATALOG_DIGEST_MISMATCH"
    finally:
        opened.close()


def test_a_responses_lite_model_is_refused() -> None:
    """Lite turns on standalone web search regardless of the feature flag.

    `standalone_web_search_enabled` is `namespace_tools_enabled &&
    provider.capabilities().web_search && (use_responses_lite ||
    Feature::StandaloneWebSearch)`, so `--disable standalone_web_search` does
    not settle it for a lite model.
    """
    entry = dict(REAL_LEGACY_ENTRY, use_responses_lite=True)
    assert judge_catalog(catalog(entry), "gpt-5.4").reason == "RESPONSES_LITE_ENABLED"


def test_a_missing_or_malformed_responses_lite_flag_is_refused() -> None:
    entry = {k: v for k, v in REAL_LEGACY_ENTRY.items() if k != "use_responses_lite"}
    assert judge_catalog(catalog(entry), "gpt-5.4").reason == "RESPONSES_LITE_MALFORMED"
    for bad in ("true", 1, None, {}):
        entry = dict(REAL_LEGACY_ENTRY, use_responses_lite=bad)
        assert judge_catalog(catalog(entry), "gpt-5.4").reason == "RESPONSES_LITE_MALFORMED"


def test_the_frontier_entry_fails_on_the_first_reason_it_hits() -> None:
    """It is refused twice over: `code_mode_only` and responses lite."""
    verdict = judge_catalog(catalog(REAL_SOL_ENTRY), "gpt-5.6-sol")
    assert verdict.reason == "TOOL_MODE_CODE_MODE_ONLY"
    assert verdict.surface is not None
    assert verdict.surface.use_responses_lite is True
