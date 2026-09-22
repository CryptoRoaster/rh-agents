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
DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "view_image",
    "apps",
    "plugins",
    "browser_use",
    "computer_use",
    "code_mode",
    "multi_agent",
    "standalone_web_search",
    "token_budget",
    "sleep_tool",
    "current_time_reminder",
    "request_permissions_tool",
)

# Config overrides that close instruction sources and pin the login method.
BASE_CONFIG_OVERRIDES = (
    'forced_login_method="chatgpt"',
    "tools.web_search=false",
    "tools.update_plan=false",
    "project_doc_max_bytes=0",
    "skills.include_instructions=false",
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


def validate_launcher(launcher: CodexLauncher) -> None:
    """Refuse a launcher the minimal environment could not actually start.

    The npm entry point is a `#!/usr/bin/env node` script that re-spawns the
    platform binary, so it cannot run unless `node` is reachable on the PATH the
    child is given. The self-contained platform binary needs no interpreter.
    """
    if launcher.kind is LauncherKind.NODE_SHIM:
        if not any((entry / "node").exists() for entry in launcher.path_entries):
            raise CommandBuildError(
                EvaluationFailure.LAUNCHER_UNSUPPORTED, "NODE_NOT_ON_CHILD_PATH"
            )


def build_arguments(
    *,
    launcher: CodexLauncher,
    working_directory: Path,
    schema_path: Path,
    model: str,
    effort: str | None,
    instructions: str,
    model_catalog_path: Path,
    forbidden_roots: tuple[Path, ...] = (),
) -> list[str]:
    """Assemble one non-interactive invocation that reads its data from stdin.

    `working_directory` must sit outside every forbidden root. A read-only
    sandbox is a write boundary, not a read boundary, so keeping the repository
    out of the workspace matters even when no reading tool is enabled.
    """
    validate_launcher(launcher)
    if not model_catalog_path.is_absolute():
        # `model_catalog_json` is an AbsolutePathBuf in the CLI. A relative path
        # would be rejected there, or resolved against a different directory,
        # and the pin would silently not be the file that was judged.
        raise CommandBuildError(
            EvaluationFailure.TOOL_SURFACE_UNSUPPORTED, "CATALOG_PATH_NOT_ABSOLUTE"
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
    arguments += ["-c", f"model_catalog_json={toml_string(str(model_catalog_path))}"]
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
    return [str(launcher.executable), "login", "status"]


def build_version_arguments(*, launcher: CodexLauncher) -> list[str]:
    """Assemble the build-identification probe.

    Also its own process with its own budget. What this returns decides whether
    an attempt may run at all, so it asks the launcher on disk rather than
    trusting a configured string.
    """
    validate_launcher(launcher)
    return [str(launcher.executable), "--version"]
