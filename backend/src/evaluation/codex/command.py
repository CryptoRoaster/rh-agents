"""Build the exact argument list and child environment for one attempt.

Everything here is a pure function over explicit inputs, so the shape of the
invocation is testable without starting anything.

Read the scope of these tests honestly. They prove what *we* pass. They do not
prove what the CLI then offers the model: a future build can enable a tool by
default, rename a feature flag, or ignore an override, and an argument test
would stay green through all three. That is why `models.SUPPORTED_CLI_VERSION`
exists and why the client refuses an unrecognised build outright.

The child environment is built from nothing rather than filtered. Inheritance is
the failure mode here: `shell_environment_policy` governs the environment Codex
hands to shell-like tools, not the environment Codex itself runs in, so the
parent's variables would otherwise reach the CLI untouched. Three variables can
substitute a credential (`OPENAI_API_KEY`, `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`)
and three can redirect an auth endpoint (`CODEX_REFRESH_TOKEN_URL_OVERRIDE`,
`CODEX_REVOKE_TOKEN_URL_OVERRIDE`, `CODEX_APP_SERVER_LOGIN_CLIENT_ID`). None of
them can be omitted by accident from a list that starts empty.

`forced_login_method="chatgpt"` is the second layer, never the first. Codex logs
out when a forced method conflicts with an active API-key session, so an
inherited key plus a forced method could destroy a stored ChatGPT login. The
allowlist prevents the conflict; the forced method only confirms it.
"""

from pathlib import Path

from src.evaluation.codex.models import CodexLauncher, EvaluationFailure, LauncherKind

# Feature flags this harness turns off. Each one is a tool or instruction
# channel that `core/src/tools/spec_plan.rs` gates on a feature.
# Feature flags this harness turns off. Each one is a tool, an agent, a memory
# or an instruction channel that 0.153.4 gates on a feature, and each key was
# checked against `codex features list` of the supported build.
#
# The list deliberately mirrors what Codex itself disables for a minimised
# structured turn in `tui/src/temporary_structured_request.rs`, plus the
# surfaces that request does not need to consider. Several of these default to
# ON: `image_generation`, `tool_suggest`, `shell_snapshot`, `goals`, `hooks`,
# `skill_search`, `apps`, `plugins`, `browser_use`, `computer_use`,
# `sleep_tool`, `unified_exec`, `shell_tool` and `multi_agent` are all stable
# and enabled by default, so leaving any of them out would leave it on.
DISABLED_FEATURES = (
    "apps",
    "browser_use",
    "code_mode",
    "code_mode_only",
    "computer_use",
    "context_management",
    "current_time_reminder",
    "deferred_executor",
    "enable_fanout",
    "goals",
    "hooks",
    "image_generation",
    "memories",
    "multi_agent",
    "multi_agent_v2",
    "plugins",
    "request_permissions_tool",
    "shell_snapshot",
    "shell_tool",
    "skill_search",
    "sleep_tool",
    "standalone_web_search",
    "token_budget",
    "tool_suggest",
    "unified_exec",
    "view_image",
    "web_search_cached",
    "web_search_request",
)

# Where credentials are read from and written to. `AuthCredentialsStoreMode` is
# one of file / keyring / auto / ephemeral, and the default reaches the login
# keychain -- shared with the real session and outliving any temporary home.
# `file` keeps every credential read and write inside the isolated `auth.json`:
# the login probe then judges the copy the attempt would use rather than a
# keychain entry the isolated home does not contain, and a token refresh either
# lands in that throwaway copy -- the one path the outer profile grants write
# access to -- or fails. Neither outcome can reach the real login.
AUTH_STORE_OVERRIDE = 'cli_auth_credentials_store="file"'

# Config overrides that close instruction sources and pin the login method.
BASE_CONFIG_OVERRIDES = (
    'forced_login_method="chatgpt"',
    AUTH_STORE_OVERRIDE,
    # The top-level mode is the real control. `tools.web_search` is a
    # `WebSearchToolConfig` (domains, context size, location), and its legacy
    # boolean form is parsed and then discarded -- `Some(Enabled(enabled)) =>
    # { let _ = enabled; None }` -- so `tools.web_search=false` disables
    # nothing. Without an explicit mode the resolver falls back to
    # `WebSearchMode::Cached`, and the default provider advertises web search,
    # so hosted search could still be registered.
    'web_search="disabled"',
    # `UpdatePlanToolConfig { enabled }`, so the toggle is one level down.
    "tools.update_plan.enabled=false",
    # `ExperimentalRequestUserInput { enabled }` defaults to **true**:
    # `.is_none_or(|config| config.enabled)`. Silence here means on.
    "tools.experimental_request_user_input.enabled=false",
    "project_doc_max_bytes=0",
    "skills.include_instructions=false",
    "orchestrator.skills.enabled=false",
    "orchestrator.mcp.enabled=false",
    "mcp_servers={}",
    'approval_policy="never"',
)

# Flags that must never appear. `--ignore-rules` is on this list deliberately:
# `.rules` files are execpolicy documents that *restrict* commands, so ignoring
# them loosens the run instead of hardening it.
FORBIDDEN_ARGUMENTS = (
    "--ignore-rules",
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust",
    "--approve-for-me",
    "--add-dir",
    "--oss",
    "resume",
    "fork",
)

ALLOWED_ENVIRONMENT_KEYS = ("CODEX_HOME", "HOME", "PATH", "TMPDIR", "LANG")

# `/dev/fd/<n>` resolves through the open file description rather than a
# directory entry, which is what makes the catalog pin about bytes.
FD_REFERENCE_PREFIX = "/dev/fd/"


def toml_string(value: str) -> str:
    """Quote a value as a TOML basic string.

    `-c key=value` parses the value as TOML and silently falls back to treating
    it as a literal when parsing fails. Relying on that fallback would make the
    meaning of an instruction block depend on whether it happens to look like
    TOML, so the value is always emitted as a well-formed basic string.
    """
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


class CommandBuildError(Exception):
    """The invocation could not be built safely."""

    def __init__(self, failure: EvaluationFailure, reason_code: str) -> None:
        self.failure = failure
        self.reason_code = reason_code
        super().__init__(f"{failure.value}:{reason_code}")


def child_environment(
    *,
    codex_home: Path,
    home: Path,
    tmpdir: Path,
    path_entries: tuple[Path, ...],
) -> dict[str, str]:
    """Return the complete child environment. Exactly the allowed keys, no more."""
    return {
        "CODEX_HOME": str(codex_home),
        "HOME": str(home),
        "PATH": ":".join(str(entry) for entry in path_entries),
        "TMPDIR": str(tmpdir),
        "LANG": "en_US.UTF-8",
    }


# The first bytes of a Mach-O executable, in the four forms macOS produces.
# A fake launcher is a script; a real Codex build is one of these.
MACH_O_MAGIC = (
    b"\xcf\xfa\xed\xfe",  # 64-bit, little endian
    b"\xce\xfa\xed\xfe",  # 32-bit, little endian
    b"\xca\xfe\xba\xbe",  # universal (fat)
    b"\xbe\xba\xfe\xca",  # universal, byte-swapped
)


def looks_like_a_native_binary(path: Path) -> bool:
    """Whether the file on disk is a compiled executable rather than a script."""
    try:
        with open(path, "rb") as handle:
            return handle.read(4) in MACH_O_MAGIC
    except OSError:
        return False


def validate_launcher(launcher: CodexLauncher) -> None:
    """Refuse a launcher the minimal environment could not actually start.

    The npm entry point is a `#!/usr/bin/env node` script that re-spawns the
    platform binary, so it cannot run unless `node` is reachable on the PATH the
    child is given. The self-contained platform binary needs no interpreter.

    `FAKE_EXECUTABLE` additionally has to *be* a fake. The kind decides whether
    a release permit is required, so a compiled binary declared as a fake would
    turn the permit requirement into an opt-out. This does not stop a caller who
    sets out to defeat it -- a wrapper script around the real binary would pass
    -- and it is not meant to. It stops the accident.
    """
    if launcher.kind is LauncherKind.NODE_SHIM:
        if not any((entry / "node").exists() for entry in launcher.path_entries):
            raise CommandBuildError(
                EvaluationFailure.LAUNCHER_UNSUPPORTED, "NODE_NOT_ON_CHILD_PATH"
            )
    if launcher.kind is LauncherKind.FAKE_EXECUTABLE and looks_like_a_native_binary(
        launcher.executable
    ):
        raise CommandBuildError(
            EvaluationFailure.LAUNCHER_UNSUPPORTED, "NATIVE_BINARY_DECLARED_AS_FAKE"
        )


def build_arguments(
    *,
    launcher: CodexLauncher,
    working_directory: Path,
    schema_path: Path,
    model: str,
    effort: str | None,
    instructions: str,
    model_catalog_reference: str,
    forbidden_roots: tuple[Path, ...] = (),
) -> list[str]:
    """Assemble one non-interactive invocation that reads its data from stdin.

    `working_directory` must sit outside every forbidden root. A read-only
    sandbox is a write boundary, not a read boundary, so keeping the repository
    out of the workspace matters even when no reading tool is enabled.
    """
    validate_launcher(launcher)
    if not Path(model_catalog_reference).is_absolute():
        # `model_catalog_json` is an AbsolutePathBuf in the CLI. A relative path
        # would be rejected there, or resolved against a different directory,
        # and the pin would not be what was judged.
        raise CommandBuildError(
            EvaluationFailure.TOOL_SURFACE_UNSUPPORTED, "CATALOG_PATH_NOT_ABSOLUTE"
        )
    if model_catalog_reference.startswith(FD_REFERENCE_PREFIX):
        # A descriptor reference only survives if the launcher hands the
        # descriptor on unchanged. The platform binary is exec'd by our own gate
        # and does, and so does a fake that execs an interpreter; the npm entry
        # point re-spawns through Node, and that Node process passing a foreign
        # descriptor through its own spawn has not been demonstrated. Refusing
        # the shim is the fail-closed answer.
        if launcher.kind is LauncherKind.NODE_SHIM:
            raise CommandBuildError(
                EvaluationFailure.LAUNCHER_UNSUPPORTED, "FD_CATALOG_NEEDS_PLATFORM_BINARY"
            )
    resolved = working_directory.resolve()
    for root in forbidden_roots:
        if resolved == root.resolve() or root.resolve() in resolved.parents:
            raise CommandBuildError(
                EvaluationFailure.LAUNCHER_UNSUPPORTED, "WORKSPACE_INSIDE_FORBIDDEN_ROOT"
            )

    arguments = [
        str(launcher.executable),
        "exec",
        "--json",
        "--ignore-user-config",
        "--ephemeral",
        "--skip-git-repo-check",
        "--color",
        "never",
        "--cd",
        str(resolved),
        "--sandbox",
        "read-only",
        "--output-schema",
        str(schema_path),
        "--model",
        model,
    ]
    for feature in DISABLED_FEATURES:
        arguments += ["--disable", feature]
    for override in BASE_CONFIG_OVERRIDES:
        arguments += ["-c", override]
    if effort is not None:
        arguments += ["-c", f"model_reasoning_effort={toml_string(effort)}"]
    # Pins the run's model catalog to one file. `config_model_catalog` makes the
    # provider build a `StaticModelsManager`, which ignores the refresh strategy,
    # never consults the on-disk cache and treats `refresh_if_new_etag` as a
    # no-op -- so the entry this harness judged is the entry the turn uses.
    arguments += ["-c", f"model_catalog_json={toml_string(model_catalog_reference)}"]
    arguments += ["-c", f"developer_instructions={toml_string(instructions)}"]
    # A bare "-" makes the CLI read the prompt from stdin, so untrusted market
    # data never lands in argv, which is readable process-wide.
    arguments.append("-")
    return arguments


def build_preflight_arguments(*, launcher: CodexLauncher) -> list[str]:
    """Assemble the login-status probe.

    A separate process from the attempt, counted and bounded separately, and run
    with the same scrubbed environment. It starts no turn and calls no model.
    """
    validate_launcher(launcher)
    # The store is pinned here too. Without it the probe could answer from the
    # login keychain while the attempt reads the isolated `auth.json`, and a
    # session would be reported that the turn never gets to use.
    return [str(launcher.executable), "login", "status", "-c", AUTH_STORE_OVERRIDE]


def build_version_arguments(*, launcher: CodexLauncher) -> list[str]:
    """Assemble the build-identification probe.

    Also its own process with its own budget. What this returns decides whether
    an attempt may run at all, so it asks the launcher on disk rather than
    trusting a configured string.
    """
    validate_launcher(launcher)
    return [str(launcher.executable), "--version"]
