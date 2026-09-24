"""One live environment that is prepared, measured, and only then authorised.

The previous preflight built a temporary tree, measured it, and deleted the
whole thing before returning a `PreflightStatus`. That made the result
structurally unable to say anything about a later run: the isolated home it
judged was gone, the profile it measured was gone, and whatever ran afterwards
would run in a different tree behind a freshly composed profile. A green
preflight about a directory that no longer exists is not evidence.

So the environment is now held open for as long as the authorization is valid:

    async with prepare_real_run(launcher=..., source_codex_home=...) as prepared:
        prepared.status        # what was measured
        prepared.authorization # None when anything blocking failed
        prepared.runner        # None likewise; otherwise bound to this tree

On entry: a `0700` temporary root, a workspace, an isolated `CODEX_HOME` with
the copied `auth.json`, the judged model catalog written once into a private
`catalog-runtime/models.json`, **one** profile written once and bound by the
digest of its bytes, the version and login probes run behind that profile in
that home, the boundary measured, the gates evaluated, and -- only if nothing
blocking failed -- a `RunBinding` taken from the configuration that would
actually run.

The catalog is held open by *name* for exactly as long as the authorization is
valid, which is the one thing the previous descriptor transport could not do.
Codex 0.153.4 loads `model_catalog_json` twice -- in the initial
`ConfigBuilder::build()` and again at `thread/start` through
`ConfigManager::load_with_overrides` -- and a `/dev/fd/N` stream that the first
load consumed parses as an empty document on the second. The name is the point;
the file is 0400 in a 0500 directory, the profile grants read and nothing else
on that one literal path, and the digest is bound and re-checked before exec.

On exit: the runtime catalog, the profile, the isolated home and the whole tree
are removed.

The boundary probe keeps a home of its own. Its write check rewrites the auth
file it is pointed at, and pointing that at the copied login state would push a
real token through a shell round trip. The profile is parameterised, so the
same bytes are measured either way -- and the real `-D` parameters are part of
the binding, so what the placeholder run cannot show is exactly what the
binding pins.

No turn is started here, and no value from `auth.json` is read into this
process.
"""

import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from src.evaluation.codex import sandbox
from src.evaluation.codex.auth_home import (
    AUTH_FILE,
    AUTH_MODE,
    HOME_MODE,
    INSTALLATION_ID_FILE,
    INSTALLATION_ID_MODE,
    IsolatedHome,
    build_isolated_home,
)
from src.evaluation.codex.catalog import (
    RUNTIME_CATALOG_FILE,
    RuntimeCatalog,
    materialise_runtime_catalog,
)
from src.evaluation.codex.catalogs import (
    GPT_5_5_CATALOG,
    GPT_5_5_CATALOG_SHA256,
    GPT_5_5_CATALOG_SLUG,
)
from src.evaluation.codex.client import CodexClientConfig, binding_for
from src.evaluation.codex.command import (
    build_preflight_arguments,
    build_version_arguments,
    child_environment,
)
from src.evaluation.codex.deadline import Deadline
from src.evaluation.codex.models import (
    SUPPORTED_CLI_VERSION,
    CodexLauncher,
    OutputLimits,
    ProcessLimits,
)
from src.evaluation.codex.preflight import check_chatgpt_login, check_cli_version
from src.evaluation.codex.process import ProcessError
from src.evaluation.codex.real_run import RealCodexRunner
from src.evaluation.codex.release import (
    PreflightStatus,
    ReleaseAuthorization,
    ReleaseRefused,
    authorize,
    evaluate_release,
)

PROBE_BUDGET_SECONDS = 30.0
PLACEHOLDER_AUTH = '{"placeholder": "not a credential"}\n'
# A fixed nil-ish UUID. The machine's own installation identifier is never
# used for a destructive probe.
PLACEHOLDER_INSTALLATION_ID = "00000000-0000-4000-8000-000000000000"


@dataclass(frozen=True)
class PreparedRealRun:
    """A measured environment, and what it does or does not authorise.

    `authorization` and `runner` are `None` together: either the preflight
    cleared and both exist, or it did not and neither does. There is no third
    state where a caller holds a runner whose preflight failed.
    """

    root: Path
    status: PreflightStatus
    profile: sandbox.WrittenProfile
    config: CodexClientConfig
    runtime_catalog: RuntimeCatalog | None
    authorization: ReleaseAuthorization | None
    runner: RealCodexRunner | None

    def require_runner(self) -> RealCodexRunner:
        """The runner, or the refusal that explains why there is none."""
        if self.runner is None:
            raise ReleaseRefused(self.status.blocking)
        return self.runner


@dataclass(frozen=True)
class _Tree:
    """The throwaway layout, made in one place so teardown matches setup."""

    root: Path
    workspace: Path
    scratch: Path
    home: Path
    tmpdir: Path
    catalog_runtime: Path
    probe_workspace: Path
    probe_home: Path
    probe_outside: Path
    probe_catalog_runtime: Path

    @property
    def probe_catalog_file(self) -> Path:
        return self.probe_catalog_runtime / RUNTIME_CATALOG_FILE


def _build_tree(root: Path) -> _Tree:
    names = (
        "workspace",
        "scratch",
        "home",
        "tmp",
        # The runtime catalog lives here rather than in the workspace. The
        # workspace is the model's working environment; the catalog is harness
        # configuration, and the two are kept apart so that widening one never
        # widens the other.
        "catalog-runtime",
        "probe-workspace",
        "probe-outside",
        "probe-catalog-runtime",
    )
    made = {}
    for name in names:
        directory = root / name
        directory.mkdir(mode=0o700, parents=False, exist_ok=False)
        made[name] = directory

    probe_home = root / "probe-home"
    probe_home.mkdir(mode=HOME_MODE, parents=False, exist_ok=False)
    placeholder = probe_home / AUTH_FILE
    placeholder.write_text(PLACEHOLDER_AUTH, encoding="utf-8")
    os.chmod(placeholder, AUTH_MODE)
    # The probe home models both writable files, so the measurement is about
    # the policy the real home runs under rather than about half of it.
    marker = probe_home / INSTALLATION_ID_FILE
    marker.write_text(PLACEHOLDER_INSTALLATION_ID, encoding="utf-8")
    os.chmod(marker, INSTALLATION_ID_MODE)

    return _Tree(
        root=root,
        workspace=made["workspace"],
        scratch=made["scratch"],
        home=made["home"],
        tmpdir=made["tmp"],
        catalog_runtime=made["catalog-runtime"],
        probe_workspace=made["probe-workspace"],
        probe_home=probe_home,
        probe_outside=made["probe-outside"],
        probe_catalog_runtime=made["probe-catalog-runtime"],
    )


async def _ask_the_cli(
    *,
    launcher: CodexLauncher,
    roots: sandbox.SandboxRoots,
    profile: sandbox.WrittenProfile,
    environment: dict[str, str],
    workspace: Path,
) -> tuple[str | None, bool | None]:
    """Run both probes behind this profile, in this home. No turn, no model."""
    limits = OutputLimits()

    def budget() -> Deadline:
        return Deadline(total_seconds=PROBE_BUDGET_SECONDS, cleanup_reserve_seconds=2.0)

    try:
        reported = await check_cli_version(
            arguments=sandbox.wrap(build_version_arguments(launcher=launcher), profile.path, roots),
            environment=environment,
            working_directory=workspace,
            limits=limits,
            deadline=budget(),
        )
        version: str | None = reported.version
    except ProcessError:
        version = None

    try:
        login = await check_chatgpt_login(
            arguments=sandbox.wrap(
                build_preflight_arguments(launcher=launcher), profile.path, roots
            ),
            environment=environment,
            working_directory=workspace,
            limits=limits,
            deadline=budget(),
        )
        session: bool | None = login.chatgpt_session
    except ProcessError:
        # `login status` exits non-zero when there is no session, and the
        # process layer reports a non-zero exit as a failure. That is an
        # answer -- not logged in -- rather than an absence of one.
        session = False
    return version, session


def _configuration(
    *,
    launcher: CodexLauncher,
    tree: _Tree,
    isolated: IsolatedHome,
    roots: sandbox.SandboxRoots,
    profile: sandbox.WrittenProfile,
    runtime_catalog: RuntimeCatalog | None,
) -> CodexClientConfig:
    """Exactly the configuration that was measured, and the one that is bound."""
    return CodexClientConfig(
        launcher=launcher,
        codex_home=isolated.path,
        home=tree.home,
        tmpdir=tree.tmpdir,
        workspace=tree.workspace,
        scratch=tree.scratch,
        model_catalog_path=GPT_5_5_CATALOG,
        expected_catalog_sha256=GPT_5_5_CATALOG_SHA256,
        runtime_catalog=runtime_catalog,
        model=GPT_5_5_CATALOG_SLUG,
        process_limits=ProcessLimits(),
        run_preflight=True,
        outer_sandbox=roots,
        outer_profile=profile,
    )


def _reopen_for_teardown(directory: Path) -> None:
    """Restore the modes a read-only catalog directory was locked down to."""
    try:
        directory.chmod(0o700)
        for item in directory.iterdir():
            item.chmod(0o600)
    except OSError:
        pass


@asynccontextmanager
async def prepare_real_run(
    *, launcher: CodexLauncher, source_codex_home: Path
) -> AsyncIterator[PreparedRealRun]:
    """Build the environment, measure it, and hold it open while it is valid."""
    root = Path(tempfile.mkdtemp(prefix="codex-preflight-"))
    os.chmod(root, 0o700)
    tree = _build_tree(root)
    profile = sandbox.write_bound_profile(root)
    isolated = build_isolated_home(source_codex_home, root)
    # One materialisation, held for the whole context. The file keeps its name
    # until teardown precisely because Codex reopens it: the catalog is loaded
    # once by the initial `ConfigBuilder::build()` and again at `thread/start`.
    runtime_catalog = materialise_runtime_catalog(GPT_5_5_CATALOG, tree.catalog_runtime)
    vendor = sandbox.codex_vendor_root(launcher.executable)
    try:
        version: str | None = None
        session: bool | None = None
        roots = sandbox.SandboxRoots(
            codex_vendor=vendor,
            workspace=tree.workspace,
            codex_home=isolated.path if isolated is not None else tree.probe_home,
            # Falls back to the placeholder when the runtime catalog could not
            # be written, so the profile still has every parameter it names.
            # The gates then fail on the catalog rather than on a malformed
            # `sandbox-exec` invocation.
            catalog_file=(
                runtime_catalog.path if runtime_catalog is not None else tree.probe_catalog_file
            ),
        )
        if isolated is not None:
            version, session = await _ask_the_cli(
                launcher=launcher,
                roots=roots,
                profile=profile,
                environment=child_environment(
                    codex_home=isolated.path,
                    home=tree.home,
                    tmpdir=tree.tmpdir,
                    path_entries=launcher.path_entries,
                ),
                workspace=tree.workspace,
            )

        status = evaluate_release(
            catalog_path=GPT_5_5_CATALOG,
            expected_digest=GPT_5_5_CATALOG_SHA256,
            model=GPT_5_5_CATALOG_SLUG,
            snapshot_dir=tree.scratch,
            cli_version=version,
            supported_version=SUPPORTED_CLI_VERSION,
            chatgpt_session=session,
            # Measured against the placeholder home, for the reason in the
            # module docstring. Same profile bytes; different `-D` values,
            # and the real ones are what the binding pins.
            roots=sandbox.SandboxRoots(
                codex_vendor=vendor,
                workspace=tree.probe_workspace,
                codex_home=tree.probe_home,
                # A placeholder, never the runtime catalog. The catalog checks
                # in the boundary probe try to write, truncate and unlink the
                # file they are pointed at; they are measurements only because
                # the policy refuses them, and a hole in that policy must not
                # cost the run its catalog.
                catalog_file=tree.probe_catalog_file,
            ),
            probe_outside=tree.probe_outside,
            isolated_home=isolated,
        )

        config = _configuration(
            launcher=launcher,
            tree=tree,
            isolated=isolated if isolated is not None else IsolatedHome(tree.probe_home, False),
            roots=roots,
            profile=profile,
            runtime_catalog=runtime_catalog,
        )
        authorization: ReleaseAuthorization | None = None
        runner: RealCodexRunner | None = None
        try:
            authorization = authorize(status, binding_for(config, profile.digest))
            runner = RealCodexRunner(authorization=authorization, config=config)
        except ReleaseRefused:
            authorization = None
            runner = None

        yield PreparedRealRun(
            root=root,
            status=status,
            profile=profile,
            config=config,
            runtime_catalog=runtime_catalog,
            authorization=authorization,
            runner=runner,
        )
    finally:
        if isolated is not None:
            isolated.discard()
        if runtime_catalog is not None:
            # Its own teardown, because the directory is 0500 and the file is
            # 0400 by the time anyone gets here; `rmtree` alone would leave
            # both behind.
            runtime_catalog.discard()
        # The probe's placeholder catalog is left in the same read-only shape,
        # for the same reason. `rmtree(ignore_errors=True)` would silently
        # abandon the whole tree over one unwritable directory.
        _reopen_for_teardown(tree.probe_catalog_runtime)
        profile.path.unlink(missing_ok=True)
        shutil.rmtree(root, ignore_errors=True)


__all__ = ["PLACEHOLDER_AUTH", "PreparedRealRun", "prepare_real_run"]
