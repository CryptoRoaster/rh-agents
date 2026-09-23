"""The preflight that is actually run, end to end, without contacting a model.

Thin on purpose. `prepared_run.prepare_real_run` owns the whole lifecycle --
the temporary tree, the isolated home, the one bound profile, both CLI probes
behind that profile, the boundary measurement and the gate evaluation -- and
this module is the command that enters it, prints what was measured, and lets
it be torn down again.

It starts no turn. Exit code 0 means every blocking gate passed and a runner
could be constructed for *this* environment; it does not mean anything ran, and
running something is a separate, separately reviewed decision.
"""

import argparse
import asyncio
import os
import shutil
from pathlib import Path

from src.evaluation.codex.catalogs import GPT_5_5_CATALOG, GPT_5_5_CATALOG_SLUG
from src.evaluation.codex.models import CodexLauncher, LauncherKind
from src.evaluation.codex.prepared_run import prepare_real_run
from src.evaluation.codex.release import PreflightStatus

CATALOG_PATH = GPT_5_5_CATALOG
MODEL = GPT_5_5_CATALOG_SLUG


async def run_preflight(
    *, launcher: CodexLauncher, source_codex_home: Path
) -> tuple[PreflightStatus, bool]:
    """Prepare, measure, and report whether a runner existed inside the context.

    The second value is deliberately observed *within* the context. Asking
    afterwards would be asking about a tree that no longer exists, which is the
    shape of claim this whole rework was about.
    """
    async with prepare_real_run(launcher=launcher, source_codex_home=source_codex_home) as prepared:
        return prepared.status, prepared.runner is not None


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
    status, authorised = asyncio.run(
        run_preflight(launcher=launcher, source_codex_home=arguments.source_codex_home)
    )
    print(status.render())
    print()
    print(f"RUNNER_BOUND_TO_THIS_ENVIRONMENT  {'YES' if authorised else 'NO'}")
    print("REAL_CODEX_RUN_STATUS             BLOCKED_PENDING_FINAL_INDEPENDENT_REVIEW")
    return 0 if authorised else 1


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())


__all__ = ["CATALOG_PATH", "MODEL", "default_launcher", "run_preflight"]
