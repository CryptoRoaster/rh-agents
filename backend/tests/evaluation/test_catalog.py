"""The catalog guard, including what the real 0.153.4 catalog actually says."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from src.evaluation.codex import catalog as catalog_module
from src.evaluation.codex.catalog import (
    judge_catalog,
    judge_snapshot,
    snapshot_catalog,
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


def test_a_missing_catalog_file_cannot_be_snapshotted(tmp_path: Path) -> None:
    assert snapshot_catalog(tmp_path / "absent.json", tmp_path) is None


def test_the_judged_bytes_survive_the_path_being_replaced(tmp_path: Path) -> None:
    """The binding is to content, not to a name.

    A guard that re-read the path before the run would have a window between the
    two reads. Holding the descriptor closes it: the bytes stay reachable
    through `/dev/fd/<n>` no matter what the directory entry becomes.
    """
    path = tmp_path / "catalog.json"
    path.write_text(catalog(REAL_LEGACY_ENTRY), encoding="utf-8")
    opened = snapshot_catalog(path, tmp_path)
    assert opened is not None
    try:
        swap = tmp_path / "swap.json"
        swap.write_text(
            catalog(dict(REAL_LEGACY_ENTRY, tool_mode="code_mode_only")), encoding="utf-8"
        )
        swap.replace(path)

        assert "code_mode_only" in path.read_text(encoding="utf-8")
        assert "code_mode_only" not in opened.payload
        assert judge_snapshot(opened, "gpt-5.4", opened.digest).reason is None
        assert Path(opened.reference).read_text(encoding="utf-8") == opened.payload
    finally:
        opened.close()


def test_a_digest_pin_refuses_content_that_was_not_reviewed(tmp_path: Path) -> None:
    """The whole approved snapshot is the contract, not the three fields read."""
    path = tmp_path / "catalog.json"
    path.write_text(catalog(REAL_LEGACY_ENTRY), encoding="utf-8")
    opened = snapshot_catalog(path, tmp_path)
    assert opened is not None
    try:
        assert judge_snapshot(opened, "gpt-5.4", opened.digest).reason is None
        assert judge_snapshot(opened, "gpt-5.4", "0" * 64).reason == "CATALOG_DIGEST_MISMATCH"
    finally:
        opened.close()


def test_a_responses_lite_model_is_accepted() -> None:
    """Lite is not a reason to refuse, and treating it as one was a mistake.

    It does feed `standalone_web_search_enabled`, but
    `append_extension_tool_executors` also requires `web_search_mode_on`, and
    `web_search="disabled"` makes that false, so the standalone executor is
    dropped whatever the model declares. Lite also *shrinks* the surface:
    `hosted_model_tool_specs` returns `Vec::new()` for a lite model.
    """
    entry = dict(REAL_LEGACY_ENTRY, use_responses_lite=True)
    verdict = judge_catalog(catalog(entry), "gpt-5.4")
    assert verdict.reason is None
    assert verdict.surface is not None
    assert verdict.surface.use_responses_lite is True


def test_a_missing_or_malformed_responses_lite_flag_is_refused() -> None:
    entry = {k: v for k, v in REAL_LEGACY_ENTRY.items() if k != "use_responses_lite"}
    assert judge_catalog(catalog(entry), "gpt-5.4").reason == "RESPONSES_LITE_MALFORMED"
    for bad in ("true", 1, None, {}):
        entry = dict(REAL_LEGACY_ENTRY, use_responses_lite=bad)
        assert judge_catalog(catalog(entry), "gpt-5.4").reason == "RESPONSES_LITE_MALFORMED"


def test_the_frontier_entry_is_refused_for_its_tool_mode_alone() -> None:
    """One reason, not two: `code_mode_only`. Lite is not a refusal."""
    verdict = judge_catalog(catalog(REAL_SOL_ENTRY), "gpt-5.6-sol")
    assert verdict.reason == "TOOL_MODE_CODE_MODE_ONLY"
    assert verdict.surface is not None
    assert verdict.surface.use_responses_lite is True


def test_short_writes_still_produce_the_exact_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single `os.write` is not a promise to write everything.

    The kernel may take fewer bytes than offered. Without a write-all loop the
    snapshot would be a truncated copy of what the guard judged, and the digest
    would still match the original because it was taken before the copy.
    """
    path = tmp_path / "catalog.json"
    path.write_text(catalog(REAL_LEGACY_ENTRY), encoding="utf-8")
    real_write = os.write

    def dribble(fd: int, data: object) -> int:
        view = memoryview(bytes(data))  # type: ignore[arg-type]
        return real_write(fd, view[:7])

    monkeypatch.setattr(catalog_module.os, "write", dribble)
    opened = snapshot_catalog(path, tmp_path)
    monkeypatch.undo()

    assert opened is not None
    try:
        raw = path.read_bytes()
        assert opened.digest == hashlib.sha256(raw).hexdigest()
        with open(opened.reference, "rb") as handle:
            assert handle.read() == raw
    finally:
        opened.close()


def test_a_write_that_makes_no_progress_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(catalog(REAL_LEGACY_ENTRY), encoding="utf-8")
    monkeypatch.setattr(catalog_module.os, "write", lambda *_a, **_k: 0)
    assert snapshot_catalog(path, tmp_path) is None


def test_a_snapshot_that_cannot_be_unlinked_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Required, not best effort: a name left behind is a way back in."""
    path = tmp_path / "catalog.json"
    path.write_text(catalog(REAL_LEGACY_ENTRY), encoding="utf-8")

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("unlink refused")

    monkeypatch.setattr(catalog_module.os, "unlink", refuse)
    assert snapshot_catalog(path, tmp_path) is None


def test_a_snapshot_that_reads_back_differently_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The finished object is verified, not just the call that produced it."""
    path = tmp_path / "catalog.json"
    path.write_text(catalog(REAL_LEGACY_ENTRY), encoding="utf-8")
    real_write = os.write

    def drop_the_tail(fd: int, data: object) -> int:
        payload = bytes(data)  # type: ignore[arg-type]
        real_write(fd, payload[:-1])
        return len(payload)  # claim success, deliver less

    monkeypatch.setattr(catalog_module.os, "write", drop_the_tail)
    assert snapshot_catalog(path, tmp_path) is None


def test_invalid_utf8_is_refused_rather_than_repaired(tmp_path: Path) -> None:
    """`read_to_string` rejects it, so judging a repaired text would be wrong."""
    path = tmp_path / "catalog.json"
    path.write_bytes(b'{"models": [\xff\xfe]}')
    assert snapshot_catalog(path, tmp_path) is None


def test_a_catalog_past_the_size_bound_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_bytes(b"x" * (catalog_module.MAX_CATALOG_BYTES + 1))
    assert snapshot_catalog(path, tmp_path) is None
    path.write_bytes(catalog(REAL_LEGACY_ENTRY).encode("utf-8"))
    opened = snapshot_catalog(path, tmp_path)
    assert opened is not None
    opened.close()
