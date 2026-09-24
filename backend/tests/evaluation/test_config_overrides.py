"""Prove the overrides the harness emits are valid for Codex 0.153.4.

Every `-c` value is parsed as TOML and then deserialised into the CLI's config
types, and a misspelled key or a wrong shape can be accepted and ignored rather
than rejected. `tools.web_search=false` was exactly that: syntactically fine,
semantically inert. The only way to know is to hand the whole set to the
supported build and see it load.

`codex debug models --bundled` does that and nothing else. It loads the config,
applies every override, and prints the catalog compiled into the binary -- no
turn, no model request, no network. The test skips when that build is not
installed, because it checks the CLI rather than this repository.
"""

import shutil
import subprocess

import pytest

from src.evaluation.codex.command import BASE_CONFIG_OVERRIDES, DISABLED_FEATURES
from src.evaluation.codex.models import SUPPORTED_CLI_VERSION

CODEX = shutil.which("codex")


def installed_version() -> str | None:
    if CODEX is None:
        return None
    try:
        result = subprocess.run(
            [CODEX, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment
        return None
    return result.stdout.strip() or result.stderr.strip()


VERSION = installed_version()
SUPPORTED = VERSION is not None and SUPPORTED_CLI_VERSION in VERSION


@pytest.mark.skipif(not SUPPORTED, reason=f"Codex {SUPPORTED_CLI_VERSION} is not installed")
def test_every_override_the_harness_emits_is_accepted() -> None:
    assert CODEX is not None
    command = [CODEX, "debug", "models", "--bundled"]
    for feature in DISABLED_FEATURES:
        command += ["--disable", feature]
    for override in BASE_CONFIG_OVERRIDES:
        command += ["-c", override]

    result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr[:2000]
    assert result.stderr.strip() == "", result.stderr[:2000]
    # A loaded config and a printed catalog: the overrides were understood, not
    # merely tolerated at the argument layer.
    assert '"models"' in result.stdout
