"""The preflight that is actually run, end to end, without contacting a model.

`release.evaluate_release` decides; this module supplies it with measurements
instead of assumptions. Three of them need a real process, and all three run
behind the same outer Seatbelt profile the attempt would use:

* `codex --version`, so the build is the one on disk rather than a setting;
* `codex login status`, in the isolated `CODEX_HOME`, with the credential store
  pinned to `file` -- otherwise the answer could come from the login keychain
  while the attempt reads the copied `auth.json`;
* the boundary probe, which measures what the profile permits.

The boundary probe gets a home of its own. It rewrites the auth file it is
pointed at to establish that exactly one path is writable, and pointing that at
the copied login state would push a real token through a shell round trip and
could alter the copy. The profile is parameterised, so measuring it against a
placeholder home measures the profile, which is the property in question.

Nothing here starts a turn, and no value from `auth.json` is read into this
process, printed or reported. The isolated home is removed on the way out.
"""

import argparse
import asyncio
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from src.evaluation.codex import sandbox
from src.evaluation.codex.auth_home import AUTH_MODE, HOME_MODE, build_isolated_home
from src.evaluation.codex.catalogs import (
    GPT_5_5_CATALOG,
    GPT_5_5_CATALOG_SHA256,
    GPT_5_5_CATALOG_SLUG,
)
from src.evaluation.codex.command import (
    build_preflight_arguments,
    build_version_arguments,
    child_environment,
)
from src.evaluation.codex.deadline import Deadline
from src.evaluation.codex.models import (
    SUPPORTED_CLI_VERSION,
    CodexLauncher,
    LauncherKind,
    OutputLimits,
)
from src.evaluation.codex.preflight import check_chatgpt_login, check_cli_version
from src.evaluation.codex.process import ProcessError
from src.evaluation.codex.release import PreflightStatus, evaluate_release

CATALOG_PATH = GPT_5_5_CATALOG
MODEL = GPT_5_5_CATALOG_SLUG
PROBE_BUDGET_SECONDS = 30.0


@dataclass(frozen=True)
class Workspaces:
    """The throwaway tree the preflight builds and then removes."""

    root: Path
    workspace: Path
    home: Path
    tmpdir: Path
    probe_workspace: Path
    probe_home: Path


def build_workspaces(root: Path) -> Workspaces:
    """Lay out the tree. The probe's home is separate from the copied one."""
    workspace = root / "workspace"
    home = root / "home"
    tmpdir = root / "tmp"
    probe_workspace = root / "probe-workspace"
    probe_home = root / "probe-home"
    for directory in (workspace, home, tmpdir, probe_workspace):
        directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    probe_home.mkdir(mode=HOME_MODE, parents=False, exist_ok=False)
    placeholder = probe_home / "auth.json"
    placeholder.write_text('{"placeholder": "not a credential"}\n', encoding="utf-8")
    os.chmod(placeholder, AUTH_MODE)
    return Workspaces(
        root=root,
        workspace=workspace,
        home=home,
        tmpdir=tmpdir,
        probe_workspace=probe_workspace,
        probe_home=probe_home,
    )


async def _measure(
    *,
    launcher: CodexLauncher,
    roots: sandbox.SandboxRoots,
    environment: dict[str, str],
    workspace: Path,
    profile: Path,
) -> tuple[str | None, bool | None]:
    """Run both CLI probes behind the profile and report what they answered."""
    limits = OutputLimits()
    version: str | None = None
    session: bool | None = None
    try:
        outcome = await check_cli_version(
            arguments=sandbox.wrap(build_version_arguments(launcher=launcher), profile, roots),
            environment=environment,
            working_directory=workspace,
            limits=limits,
            deadline=Deadline(total_seconds=PROBE_BUDGET_SECONDS, cleanup_reserve_seconds=2.0),
        )
        version = outcome.version
    except ProcessError:
        version = None
    try:
        login = await check_chatgpt_login(
            arguments=sandbox.wrap(build_preflight_arguments(launcher=launcher), profile, roots),
            environment=environment,
            working_directory=workspace,
            limits=limits,
            deadline=Deadline(total_seconds=PROBE_BUDGET_SECONDS, cleanup_reserve_seconds=2.0),
        )
        session = login.chatgpt_session
    except ProcessError:
        # `login status` exits non-zero when there is no session, which the
        # process layer reports as a failure. That is an answer, not an
        # absence of one: not logged in.
        session = False
    return version, session


async def run_preflight(*, launcher: CodexLauncher, source_codex_home: Path) -> PreflightStatus:
    """Build the isolated home, measure, evaluate, and take the home away again."""
    root = Path(tempfile.mkdtemp(prefix="codex-preflight-"))
    os.chmod(root, 0o700)
    spaces = build_workspaces(root)
    isolated = build_isolated_home(source_codex_home, root)
    vendor = sandbox.codex_vendor_root(launcher.executable)
    profile = sandbox.write_profile(root)
    try:
        version: str | None = None
        session: bool | None = None
        if isolated is not None:
            roots = sandbox.SandboxRoots(
                codex_vendor=vendor, workspace=spaces.workspace, codex_home=isolated.path
            )
            environment = child_environment(
                codex_home=isolated.path,
                home=spaces.home,
                tmpdir=spaces.tmpdir,
                path_entries=launcher.path_entries,
            )
            version, session = await _measure(
                launcher=launcher,
                roots=roots,
                environment=environment,
                workspace=spaces.workspace,
                profile=profile,
            )

        return evaluate_release(
            catalog_path=CATALOG_PATH,
            expected_digest=GPT_5_5_CATALOG_SHA256,
            model=MODEL,
            snapshot_dir=spaces.tmpdir,
            cli_version=version,
            supported_version=SUPPORTED_CLI_VERSION,
            chatgpt_session=session,
            roots=sandbox.SandboxRoots(
                codex_vendor=vendor,
                workspace=spaces.probe_workspace,
                codex_home=spaces.probe_home,
            ),
            probe_outside=root,
            isolated_home=isolated,
        )
    finally:
        if isolated is not None:
            isolated.discard()
        profile.unlink(missing_ok=True)
        shutil.rmtree(root, ignore_errors=True)


def default_launcher() -> CodexLauncher | None:
    """The platform binary, which is the only form allowed to carry a descriptor."""
    node_entry = shutil.which("codex")
    if node_entry is None:
        return None
    vendor = Path(node_entry).resolve().parent.parent
    for candidate in vendor.rglob("vendor/*/bin/codex"):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return CodexLauncher(kind=LauncherKind.PLATFORM_BINARY, executable=candidate)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-codex-home",
        type=Path,
        default=Path.home() / ".codex",
        help="the CODEX_HOME whose auth.json is copied into the isolated home",
    )
    arguments = parser.parse_args()
    launcher = default_launcher()
    if launcher is None:
        print("CODEX_LAUNCHER  FAIL  platform binary not found")
        return 2
    status = asyncio.run(
        run_preflight(launcher=launcher, source_codex_home=arguments.source_codex_home)
    )
    print(status.render())
    return 0 if status.may_run else 1


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())


__all__ = ["CATALOG_PATH", "MODEL", "build_workspaces", "default_launcher", "run_preflight"]
