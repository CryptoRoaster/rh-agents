"""Judge the model catalog that the attempt itself will run against.

The tool plan is not decided by our flags. `core/src/tools/mod.rs` reads the
model catalog first:

    pub(crate) fn requested_tool_mode(turn_context, model_info) -> ToolMode {
        model_info.tool_mode.unwrap_or_else(|| { ...features... })
    }

`unwrap_or_else` is the point: when the entry declares a `tool_mode`, the
feature flags are never consulted. `effective_tool_mode` downgrades `CodeMode`
to `Direct` but never `CodeModeOnly`, and `register_code_mode_executors` gates
on the mode rather than on `Feature::CodeMode`. So `--disable code_mode` does
not remove code mode from a model whose catalog says `code_mode_only` -- and
code mode is a local code execution surface, which reads files as easily as it
writes them.

Checking the *bundled* catalog would not have been enough, and that was a real
hole. A root session resolves `ModelInfo` through the `ModelsManager` with
`RefreshStrategy::OnlineIfUncached`, so a fresh cache entry or a remote
`/models` response could carry a different `tool_mode` than the catalog compiled
into the binary. Reading one catalog and running against another is not a
security check.

The binding that closes it is supported by the CLI itself. In
`model-provider/src/provider.rs`:

    fn models_manager(&self, codex_home, config_model_catalog) -> SharedModelsManager {
        match config_model_catalog {
            Some(model_catalog) => Arc::new(StaticModelsManager::new(auth, model_catalog)),
            None => { ...OpenAiModelsEndpoint, cache, remote... }
        }
    }

and `StaticModelsManager::raw_model_catalog` ignores the refresh strategy
entirely, returns the catalog it was constructed with, and implements
`refresh_if_new_etag` as a no-op. `config_model_catalog` comes from
`model_catalog_json` by way of `load_model_catalog` and
`thread_manager::build_models_manager`.

So the harness pins `codex exec` to one catalog file with
`-c model_catalog_json=<path>` and judges **that same file**. No process is
started to read it: a probe would only reintroduce the gap between what was
inspected and what runs.

Fail-closed throughout. A missing file, unreadable JSON, a missing model, a
field of the wrong type and an unknown value are all refusals. A wrong type is
never quietly read as absent -- `{"unexpected": "shape"}` is not `null`, and
treating it as `null` would turn a parsing accident into a permission.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# A catalog entry that names a tool mode overrides the feature flags, so the
# only accepted state is "no opinion": the key absent, or explicitly null.
ALLOWED_TOOL_MODE: str | None = None
# Anything listed here adds a tool the flags cannot remove.
ALLOWED_EXPERIMENTAL_TOOLS: frozenset[str] = frozenset()
# Values whose consequence has actually been read in the 0.153.4 sources.
KNOWN_APPLY_PATCH_TYPES = frozenset({"freeform", "function"})

MISSING = object()


@dataclass(frozen=True)
class ToolSurface:
    """What the pinned catalog says about one model's tool surface."""

    model: str
    tool_mode: str | None
    apply_patch_tool_type: str | None
    experimental_supported_tools: tuple[str, ...]


@dataclass(frozen=True)
class CatalogVerdict:
    """Either a surface within the allowlist, or the reason it is refused."""

    surface: ToolSurface | None
    reason: str | None


def judge_catalog_file(path: Path, model: str) -> CatalogVerdict:
    """Read the pinned catalog from disk and judge it."""
    try:
        payload = path.read_text(encoding="utf-8")
    except OSError:
        return CatalogVerdict(surface=None, reason="CATALOG_UNREADABLE")
    return judge_catalog(payload, model)


def judge_catalog(payload: str, model: str) -> CatalogVerdict:
    """Judge a catalog document for one model."""
    try:
        catalog = json.loads(payload)
    except json.JSONDecodeError:
        return CatalogVerdict(surface=None, reason="CATALOG_NOT_JSON")
    if not isinstance(catalog, dict):
        return CatalogVerdict(surface=None, reason="CATALOG_NOT_OBJECT")
    entries = catalog.get("models")
    if not isinstance(entries, list):
        return CatalogVerdict(surface=None, reason="CATALOG_HAS_NO_MODELS")

    entry = _entry_for(entries, model)
    if entry is None:
        return CatalogVerdict(surface=None, reason="MODEL_NOT_IN_CATALOG")

    tool_mode, reason = _nullable_string(entry, "tool_mode", "TOOL_MODE")
    if reason is not None:
        return CatalogVerdict(surface=None, reason=reason)
    apply_patch, reason = _nullable_string(entry, "apply_patch_tool_type", "APPLY_PATCH")
    if reason is not None:
        return CatalogVerdict(surface=None, reason=reason)
    experimental, reason = _string_list(entry, "experimental_supported_tools")
    if reason is not None:
        return CatalogVerdict(surface=None, reason=reason)

    surface = ToolSurface(
        model=model,
        tool_mode=tool_mode,
        apply_patch_tool_type=apply_patch,
        experimental_supported_tools=experimental,
    )
    return CatalogVerdict(surface=surface, reason=unsupported_reason(surface))


def unsupported_reason(surface: ToolSurface) -> str | None:
    """Return why this surface is refused, or None if it is within the allowlist."""
    if surface.tool_mode != ALLOWED_TOOL_MODE:
        # The decisive one: a declared tool mode bypasses every feature flag.
        return f"TOOL_MODE_{_code(surface.tool_mode)}"
    extra = set(surface.experimental_supported_tools) - ALLOWED_EXPERIMENTAL_TOOLS
    if extra:
        return f"EXPERIMENTAL_TOOL_{_code(sorted(extra)[0])}"
    if (
        surface.apply_patch_tool_type is not None
        and surface.apply_patch_tool_type not in KNOWN_APPLY_PATCH_TYPES
    ):
        return f"APPLY_PATCH_{_code(surface.apply_patch_tool_type)}"
    return None


def _entry_for(entries: list[Any], model: str) -> dict[str, Any] | None:
    for entry in entries:
        if isinstance(entry, dict) and entry.get("slug") == model:
            return entry
    return None


def _nullable_string(entry: dict[str, Any], field: str, code: str) -> tuple[str | None, str | None]:
    """Absent or null means absent. Any other non-string is a refusal.

    `{"unexpected": "shape"}` is not `null`. Reading it as `None` would turn a
    parsing accident into a permission, which is the opposite of fail-closed.
    """
    value = entry.get(field, MISSING)
    if value is MISSING or value is None:
        return None, None
    if isinstance(value, str) and value:
        return value, None
    return None, f"{code}_MALFORMED"


def _string_list(entry: dict[str, Any], field: str) -> tuple[tuple[str, ...], str | None]:
    """A list of strings, or a refusal. A missing list is a refusal too."""
    value = entry.get(field, MISSING)
    if value is MISSING or not isinstance(value, list):
        return (), "EXPERIMENTAL_TOOLS_MALFORMED"
    if not all(isinstance(item, str) for item in value):
        return (), "EXPERIMENTAL_TOOLS_MALFORMED"
    return tuple(str(item) for item in value), None


def _code(value: str | None) -> str:
    text = (value or "NONE").upper()
    return "".join(character if character.isalnum() else "_" for character in text)[:60]


__all__ = [
    "ALLOWED_EXPERIMENTAL_TOOLS",
    "ALLOWED_TOOL_MODE",
    "KNOWN_APPLY_PATCH_TYPES",
    "CatalogVerdict",
    "ToolSurface",
    "judge_catalog",
    "judge_catalog_file",
    "unsupported_reason",
]
