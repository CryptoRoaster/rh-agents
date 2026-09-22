"""Refuse a model whose catalog entry would widen the tool surface.

The tool plan is not decided by our flags alone. `core/src/tools/mod.rs` builds
it from the model catalog first:

    pub(crate) fn requested_tool_mode(turn_context, model_info) -> ToolMode {
        model_info.tool_mode.unwrap_or_else(|| { ...features... })
    }

`unwrap_or_else` is the whole point: when the catalog entry declares a
`tool_mode`, the feature flags are never consulted. `effective_tool_mode` then
only downgrades `CodeMode` to `Direct`, never `CodeModeOnly`, and
`register_code_mode_executors` gates on the mode rather than on
`Feature::CodeMode`. So `--disable code_mode` does not remove code mode from a
model whose catalog says `code_mode_only` -- and code mode is a local code
execution surface, which reads files as easily as it writes them.

The same catalog decides other tools: `apply_patch_tool_type` registers
`apply_patch`, and `experimental_supported_tools` can add `clock`,
`request_user_input_async` or `test_sync_tool`.

None of that is observable from a successful model answer. A tool that was
offered and left unused emits no event, so the only honest check is to read the
catalog the installed build ships and refuse anything outside a stated
allowlist. `codex debug models --bundled` prints exactly that catalog and makes
no request of any kind.

Fail-closed on purpose: an unknown field value, an unknown model or an
unreadable catalog all refuse the attempt. Being unable to check is treated the
same as checking and not liking the answer.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from src.evaluation.codex.deadline import Deadline
from src.evaluation.codex.models import CleanupReport, EvaluationFailure, OutputLimits
from src.evaluation.codex.preflight import capture_probe
from src.evaluation.codex.process import ProcessError

# A catalog entry that names a tool mode overrides the feature flags, so the
# only accepted value is "no opinion".
ALLOWED_TOOL_MODE: str | None = None
# Anything listed here adds a tool the flags cannot remove.
ALLOWED_EXPERIMENTAL_TOOLS: frozenset[str] = frozenset()
# Values whose consequence has actually been read in the 0.153.4 sources.
KNOWN_APPLY_PATCH_TYPES = frozenset({"freeform", "function"})

# The bundled catalog is roughly half a megabyte of JSON, far past the caps an
# attempt uses for a turn. Its own limits are explicit rather than shared.
CATALOG_LIMITS = OutputLimits(
    max_stdout_bytes=8_388_608,
    max_stderr_bytes=262_144,
    max_line_bytes=1_048_576,
    max_final_message_bytes=8_388_608,
)


@dataclass(frozen=True)
class ToolSurface:
    """What the bundled catalog says about one model's tool surface."""

    model: str
    tool_mode: str | None
    shell_type: str | None
    apply_patch_tool_type: str | None
    experimental_supported_tools: tuple[str, ...]


@dataclass(frozen=True)
class CatalogResult:
    surface: ToolSurface | None
    reason: str | None
    cleanup: CleanupReport


def read_tool_surface(payload: str, model: str) -> ToolSurface | None:
    """Pull one model's tool-relevant fields out of a bundled catalog dump."""
    try:
        catalog = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(catalog, dict):
        return None
    entries = catalog.get("models")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("slug") != model:
            continue
        experimental = entry.get("experimental_supported_tools")
        return ToolSurface(
            model=model,
            tool_mode=_optional_str(entry.get("tool_mode")),
            shell_type=_optional_str(entry.get("shell_type")),
            apply_patch_tool_type=_optional_str(entry.get("apply_patch_tool_type")),
            experimental_supported_tools=tuple(
                str(item) for item in experimental if isinstance(item, str)
            )
            if isinstance(experimental, list)
            else ("<malformed>",),
        )
    return None


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


async def check_tool_surface(
    *,
    arguments: list[str],
    environment: dict[str, str],
    working_directory: Path,
    deadline: Deadline,
    model: str,
) -> CatalogResult:
    """Read the bundled catalog and judge the model's tool surface."""
    output = await capture_probe(
        arguments=arguments,
        environment=environment,
        working_directory=working_directory,
        limits=CATALOG_LIMITS,
        deadline=deadline,
        failure=EvaluationFailure.TOOL_SURFACE_UNSUPPORTED,
    )
    surface = read_tool_surface(output.text(), model)
    if surface is None:
        return CatalogResult(surface=None, reason="MODEL_NOT_IN_CATALOG", cleanup=output.cleanup)
    return CatalogResult(
        surface=surface, reason=unsupported_reason(surface), cleanup=output.cleanup
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _code(value: str | None) -> str:
    text = (value or "NONE").upper()
    return "".join(character if character.isalnum() else "_" for character in text)[:60]


__all__ = [
    "ALLOWED_EXPERIMENTAL_TOOLS",
    "ALLOWED_TOOL_MODE",
    "CATALOG_LIMITS",
    "CatalogResult",
    "ProcessError",
    "ToolSurface",
    "check_tool_surface",
    "read_tool_surface",
    "unsupported_reason",
]
