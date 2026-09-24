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

So the harness pins `codex exec` to one catalog and judges **the same bytes**.
Not the operator's path -- that can be replaced between the read and the run,
and "same pathname" would be a check with a window in it. The operator's file
is read once, those bytes are judged, and they are then written into a private
runtime file that `model_catalog_json` points at for the rest of the prepared
run.

That runtime file is named rather than a descriptor, and the reason is a
property of the CLI rather than a preference. Codex 0.153.4 loads
`model_catalog_json` **twice** -- once in the initial `ConfigBuilder::build()`
and again at `thread/start`, through
`ConfigManager::load_with_overrides` -> `ConfigBuilder::build()` -- and each
load is a fresh `std::fs::read_to_string`. Reopening `/dev/fd/<n>` resolves to
the same open file description, so the second read of a descriptor that the
first read already consumed returns the empty string. The third real probe died
exactly there.

What the unlinked descriptor used to provide is provided by other means: the
runtime file is 0400 inside a 0500 directory, the outer profile grants
`file-read*` on that one literal path and no write operation whatsoever, the
path and the digest are both carried in the `RunBinding`, and the digest is
re-checked immediately before the exec. The output schema is read once, by
`load_output_schema`, and keeps the descriptor transport.

Every `model_info` field `spec_plan.rs` consults is either judged here or made
irrelevant by a gate this harness sets explicitly:

| field | how it is covered |
|---|---|
| `tool_mode` | **judged** -- outranks every feature flag |
| `experimental_supported_tools` | **judged** -- adds clock / user input / test tools |
| `use_responses_lite` | type-checked only; see below |
| `apply_patch_tool_type` | **judged** -- an unknown value is refused |
| `shell_type` | gated: `add_shell_tools` returns on `!Feature::ShellTool`
  in a short-circuiting OR, before the field is read |
| `supports_search_tool`, `web_search_tool_type` | gated: `web_search="disabled"`
  makes `web_search_mode_on` false, which drops the standalone executor, and
  hosted specs are not requested |
| `multi_agent_version` | gated: `multi_agent` and `multi_agent_v2` disabled |
| `input_modalities` | gated: image handling needs `view_image` /
  `image_generation`, both disabled |
| `model_messages` | descriptions only; registers nothing |

`use_responses_lite` is checked for type and then accepted either way. It does
enter `standalone_web_search_enabled`:

    namespace_tools_enabled && provider.capabilities().web_search
      && (model_info.use_responses_lite || Feature::StandaloneWebSearch)

but that is not the last word. `append_extension_tool_executors` also computes

    let web_search_mode_on = config.web_search_mode.value() != WebSearchMode::Disabled;
    if is_standalone_web_search && (!standalone_web_search_enabled || !web_search_mode_on) {
        continue;
    }

and the harness sets `web_search="disabled"`, so the standalone executor is
dropped whatever the model declares. In the other direction lite *reduces* the
surface: `hosted_model_tool_specs` returns `Vec::new()` immediately for a lite
model. Its remaining effect is on turn metadata. Refusing it as a security
matter would have been wrong, and an earlier version of this guard did exactly
that.

Fail-closed throughout. A missing file, unreadable JSON, a missing model, a
field of the wrong type and an unknown value are all refusals. A wrong type is
never quietly read as absent -- `{"unexpected": "shape"}` is not `null`, and
treating it as `null` would turn a parsing accident into a permission.
"""

import hashlib
import json
import os
import tempfile
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

# The approved catalog is one reviewed `ModelInfo` entry, which is a few
# kilobytes. The whole bundled catalog of the supported build is about half a
# megabyte, so this leaves ample room while keeping an arbitrarily large file
# from being read and copied.
MAX_CATALOG_BYTES = 1_048_576


@dataclass(frozen=True)
class CatalogSnapshot:
    """A private, read-only copy of the bytes that were judged.

    Keeping the operator's own descriptor would not have been enough. A
    descriptor binds to an inode, not to bytes: `open(path, O_WRONLY|O_TRUNC)`
    followed by a write changes what an already-open read handle sees, so an
    in-place edit after the check would reach the child. Replacing the path is
    only one of the two ways to do that, and the earlier fix addressed only
    that one.

    So the judged bytes are copied into a fresh file, flushed, reopened
    read-only, and then unlinked. What the child inherits has no name left to
    write through and no write permission of its own.
    """

    fd: int
    payload: str
    digest: str

    @property
    def reference(self) -> str:
        return f"/dev/fd/{self.fd}"

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


# The named runtime catalog.
#
# `model_catalog_json` is not read once. Codex 0.153.4 loads it during the
# initial `ConfigBuilder::build()` in `exec/src/lib.rs`, hands the same CLI
# overrides to `InProcessClientStartArgs`, and `thread/start` then runs
# `ConfigManager::load_with_overrides` -> `load_with_cli_overrides` ->
# `ConfigBuilder::build()` a second time. Each build calls
# `load_model_catalog` -> `load_catalog_json` -> `std::fs::read_to_string`.
#
# Reopening `/dev/fd/N` resolves to the same open file description, so the
# first full read leaves the offset at the end and the second read returns the
# empty string. That is what killed the third real probe:
#
#   failed to parse model_catalog_json path `/dev/fd/6` as JSON:
#   EOF while parsing a value at line 1 column 0
#
# So the catalog travels as a named file that can be opened again from scratch,
# while the output schema -- read once, by `load_output_schema` -- keeps the
# descriptor transport.
RUNTIME_CATALOG_FILE = "models.json"
# Read-only once the bytes are in place. The sandbox denies writes anyway; this
# is the second lock rather than the only one.
RUNTIME_CATALOG_MODE = 0o400
# No write bit on the directory either, so no sidecar can appear beside the
# catalog even outside the sandbox. Restored for the duration of cleanup.
RUNTIME_CATALOG_DIR_MODE = 0o500
RUNTIME_CATALOG_BUILD_MODE = 0o700


@dataclass(frozen=True)
class RuntimeCatalog:
    """The judged bytes, kept under a name the CLI may reopen at will.

    Everything `CatalogSnapshot` establishes about the bytes is established
    here too -- one bounded read of the operator's file, strict UTF-8, a
    completed write, an fsync, a readback and a digest. What differs is only
    what happens afterwards: the file keeps its name for as long as the
    prepared run is valid, instead of being unlinked behind an open descriptor.

    Keeping the name is what the descriptor could not offer, and it costs the
    unlink-based immutability. Three things take its place: the file is 0400
    and its directory is 0500, the sandbox profile grants `file-read*` on this
    one literal path and no write operation at all, and `still_matches` is
    re-checked immediately before the exec, so bytes that changed after the
    judgement refuse the run rather than reaching the model.
    """

    directory: Path
    path: Path
    payload: str
    digest: str

    @property
    def reference(self) -> str:
        """What `model_catalog_json` is pointed at. An absolute, named path."""
        return str(self.path)

    def still_matches(self) -> bool:
        """Re-read the file and compare. False means it changed or vanished."""
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(MAX_CATALOG_BYTES + 1)
        except OSError:
            return False
        if len(raw) > MAX_CATALOG_BYTES:
            return False
        return hashlib.sha256(raw).hexdigest() == self.digest

    def discard(self) -> None:
        """Remove the file and its directory, restoring the modes to do so."""
        _chmod(self.directory, RUNTIME_CATALOG_BUILD_MODE)
        _chmod(self.path, 0o600)
        _cleanup_unlink(str(self.path))
        try:
            os.rmdir(self.directory)
        except OSError:
            pass


def materialise_runtime_catalog(path: Path, directory: Path) -> RuntimeCatalog | None:
    """Read the operator's catalog once and freeze those bytes under a name.

    `directory` must already exist and must be private to this run. The catalog
    is harness configuration, not model working material, so it deliberately
    does not go into the evaluation workspace.
    """
    raw = _read_operator_file(path)
    if raw is None:
        return None
    return runtime_catalog_from_payload(raw, directory)


def runtime_catalog_from_payload(raw: bytes, directory: Path) -> RuntimeCatalog | None:
    """Freeze bytes the harness already holds into the named runtime file."""
    try:
        # Strict for the same reason the descriptor path is strict:
        # `load_catalog_json` uses `read_to_string`, which rejects invalid
        # UTF-8, so repairing it here would judge a text the loader never sees.
        payload = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    digest = hashlib.sha256(raw).hexdigest()
    target = directory / RUNTIME_CATALOG_FILE

    writer = -1
    try:
        os.chmod(directory, RUNTIME_CATALOG_BUILD_MODE)
        # O_EXCL: this function never writes over an existing runtime catalog.
        writer = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        _write_all(writer, raw)
        os.fsync(writer)
    except OSError:
        if writer != -1:
            _close(writer)
        _cleanup_unlink(str(target))
        return None
    _close(writer)

    # Hash the finished object rather than trusting the calls that produced it.
    if not _file_matches(target, digest):
        _cleanup_unlink(str(target))
        return None

    try:
        os.chmod(target, RUNTIME_CATALOG_MODE)
        os.chmod(directory, RUNTIME_CATALOG_DIR_MODE)
    except OSError:
        _chmod(directory, RUNTIME_CATALOG_BUILD_MODE)
        _cleanup_unlink(str(target))
        return None

    return RuntimeCatalog(
        directory=directory, path=target.resolve(), payload=payload, digest=digest
    )


def _file_matches(path: Path, digest: str) -> bool:
    """Whether the file on disk hashes to the digest that was judged."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_CATALOG_BYTES + 1)
    except OSError:
        return False
    if len(raw) > MAX_CATALOG_BYTES:
        return False
    return hashlib.sha256(raw).hexdigest() == digest


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def snapshot_catalog(path: Path, snapshot_dir: Path) -> CatalogSnapshot | None:
    """Read the operator's catalog once and freeze those exact bytes.

    Every step that could leave the snapshot different from what was judged is
    checked rather than assumed: the write is completed in a loop, the unlink
    has to succeed, and the finished file is read back and hashed before the
    descriptor is handed on. A single `os.write` is not a guarantee, and an
    unlink that quietly failed would leave a name through which the snapshot
    could still be rewritten.

    Returns None on any failure; the caller refuses. Being unable to establish
    the snapshot is the same answer as not liking it.
    """
    raw = _read_operator_file(path)
    if raw is None:
        return None
    return snapshot_payload(raw, snapshot_dir)


def snapshot_payload(raw: bytes, snapshot_dir: Path) -> CatalogSnapshot | None:
    """Freeze bytes the harness already holds, with the same guarantees.

    Used for the output schema as well as the catalog: passing the schema by
    path would mean opening the scratch directory to the sandboxed process,
    and a descriptor to an unlinked private copy costs nothing extra.
    """
    try:
        # Strict: `load_catalog_json` uses `read_to_string`, which rejects
        # invalid UTF-8. Repairing it here would mean judging a different text
        # than the runtime loader ever sees.
        payload = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    digest = hashlib.sha256(raw).hexdigest()

    temporary = _write_snapshot(raw, snapshot_dir)
    if temporary is None:
        return None

    try:
        # Read-only on purpose: the descriptor the child inherits must not
        # carry a write capability of its own.
        fd = os.open(temporary, os.O_RDONLY)
    except OSError:
        _cleanup_unlink(temporary)
        return None

    try:
        # Rare, but it has its own failure mode: without the flag the child
        # would never receive the descriptor, and letting the error escape
        # would leave both the descriptor and the temporary name behind.
        os.set_inheritable(fd, True)
    except OSError:
        _close(fd)
        _cleanup_unlink(temporary)
        return None

    try:
        # Required, not best effort. A snapshot still reachable by name is not
        # the immutable thing this function promises.
        os.unlink(temporary)
    except OSError:
        _close(fd)
        return None

    if not _matches_after_readback(fd, digest):
        _close(fd)
        return None

    return CatalogSnapshot(fd=fd, payload=payload, digest=digest)


def _read_operator_file(path: Path) -> bytes | None:
    """Read the operator's catalog, refusing anything past the size bound."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_CATALOG_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_CATALOG_BYTES:
        return None
    return raw


def _write_snapshot(raw: bytes, snapshot_dir: Path) -> str | None:
    """Write the bytes out in full, or leave nothing behind."""
    writer = -1
    temporary = ""
    try:
        writer, temporary = tempfile.mkstemp(dir=snapshot_dir, prefix="catalog-", suffix=".json")
        _write_all(writer, raw)
        os.fsync(writer)
    except OSError:
        if writer != -1:
            _close(writer)
        if temporary:
            _cleanup_unlink(temporary)
        return None
    _close(writer)
    return temporary


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte. A short write is normal; losing bytes is not."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("snapshot write made no progress")
        view = view[written:]


def _matches_after_readback(fd: int, digest: str) -> bool:
    """Hash what the finished file actually contains, then rewind.

    Verifying the object rather than the call: a short write, a truncation or
    anything else that made the snapshot differ shows up here, before the
    descriptor is handed to anyone.
    """
    chunks: list[bytes] = []
    try:
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        if hashlib.sha256(b"".join(chunks)).hexdigest() != digest:
            return False
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError:
        return False
    return True


def _close(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _cleanup_unlink(path: str) -> None:
    """Best effort, for paths that only matter while cleaning up after a failure."""
    try:
        os.unlink(path)
    except OSError:
        pass


@dataclass(frozen=True)
class ToolSurface:
    """What the pinned catalog says about one model's tool surface."""

    model: str
    tool_mode: str | None
    apply_patch_tool_type: str | None
    experimental_supported_tools: tuple[str, ...]
    use_responses_lite: bool


@dataclass(frozen=True)
class CatalogVerdict:
    """Either a surface within the allowlist, or the reason it is refused."""

    surface: ToolSurface | None
    reason: str | None


def judge_snapshot(
    catalog: CatalogSnapshot | RuntimeCatalog, model: str, expected_digest: str
) -> CatalogVerdict:
    """Judge the bytes that were actually read, digest first.

    The digest is required, not optional. It turns the approved catalog into
    reviewable content rather than an operational file a few fields get sampled
    from: the whole `ModelInfo` snapshot is what was approved, not just the
    parts this module looks at.
    """
    if catalog.digest != expected_digest:
        return CatalogVerdict(surface=None, reason="CATALOG_DIGEST_MISMATCH")
    return judge_catalog(catalog.payload, model)


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
    lite = entry.get("use_responses_lite", MISSING)
    if not isinstance(lite, bool):
        return CatalogVerdict(surface=None, reason="RESPONSES_LITE_MALFORMED")

    surface = ToolSurface(
        model=model,
        tool_mode=tool_mode,
        apply_patch_tool_type=apply_patch,
        experimental_supported_tools=experimental,
        use_responses_lite=lite,
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
    "MAX_CATALOG_BYTES",
    "RUNTIME_CATALOG_DIR_MODE",
    "RUNTIME_CATALOG_FILE",
    "RUNTIME_CATALOG_MODE",
    "CatalogSnapshot",
    "CatalogVerdict",
    "RuntimeCatalog",
    "ToolSurface",
    "judge_catalog",
    "judge_snapshot",
    "materialise_runtime_catalog",
    "runtime_catalog_from_payload",
    "snapshot_catalog",
    "unsupported_reason",
]
