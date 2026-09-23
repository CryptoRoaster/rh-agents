"""Shared scaffolding for the Codex evaluation probe tests.

Every test here drives a fake process. That bounds what they can show: they
exercise our argument building, our process handling and our validation, and
they say nothing about the real CLI's tool surface, its filesystem isolation or
how subscription usage is metered.
"""

import hashlib
import json
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from src.agents.orbit.models import OrbitAssessment, OrbitTaskInput
from src.agents.orbit.validation import OrbitValidationError, validate_assessment
from src.evaluation.codex.client import CodexClientConfig, CodexEvaluationClient
from src.evaluation.codex.models import (
    CodexLauncher,
    DomainValidationError,
    EvaluationRequest,
    LauncherKind,
    OutputLimits,
)
from tests.evaluation.fixtures.orbit_candidate import task_input

FAKE = Path(__file__).parent / "fakes" / "fake_codex.py"
CHILD_PATH_ENTRIES = (Path("/usr/bin"), Path("/bin"))

# Variables that appear in the child without the harness passing them.
# `__CF_USER_TEXT_ENCODING` is added by macOS to every process it starts.
# `PWD` and `SHLVL` are added by the `/bin/sh` shim these tests use to reach the
# fake; the real launcher is an executable and adds neither. None of them can
# carry a credential, and the assertion still requires every other key to be one
# the harness handed over.
LAUNCHER_INJECTED_ENVIRONMENT_KEYS = frozenset({"__CF_USER_TEXT_ENCODING", "PWD", "SHLVL"})


def write_launcher(directory: Path) -> Path:
    """Write a launcher that execs the fake under this test run's interpreter.

    The shim matters for one assertion. `/usr/bin/python3` on macOS is an xcrun
    wrapper that adds `SDKROOT`, `CPATH`, `LIBRARY_PATH` and `MANPATH` to its own
    child, which would show up as environment entries the harness never passed.
    Going through the current interpreter keeps the observed environment about
    what the harness actually handed over.
    """
    launcher = directory / "codex"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n', encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return launcher


def orbit_domain_validator(bound_input: OrbitTaskInput) -> Callable[[OrbitAssessment], None]:
    """Bind the original input into a validator the generic client can call.

    The client never imports ORBIT; the binding happens here, in the test that
    knows which input the answer must agree with.
    """

    def validate(output: OrbitAssessment) -> None:
        try:
            validate_assessment(output, bound_input)
        except OrbitValidationError as error:
            raise DomainValidationError(error.reason_code) from None

    return validate


class Probe:
    """One configured client plus the workspace its fake process reads."""

    def __init__(self, root: Path) -> None:
        self.workspace = root / "workspace"
        self.scratch = root / "scratch"
        self.codex_home = root / "codex-home"
        self.home = root / "home"
        self.tmpdir = root / "tmp"
        for directory in (
            self.workspace,
            self.scratch,
            self.codex_home,
            self.home,
            self.tmpdir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.launcher_path = write_launcher(root)
        self.model_catalog_path = root / "model-catalog.json"
        self.catalog()
        self.task_input = task_input()

    def catalog(self, **overrides: Any) -> None:
        """Write the catalog the attempt is pinned to.

        Its default entry is the one the harness accepts, so a test only has to
        say when it wants a wider surface. The fake's `debug models --bundled`
        output says the opposite on purpose: anything that still passes has
        judged this file and not that dump.
        """
        entry: dict[str, Any] = {
            "slug": "gpt-5.4",
            "tool_mode": None,
            "shell_type": "unified_exec",
            "apply_patch_tool_type": "freeform",
            "experimental_supported_tools": [],
            "use_responses_lite": False,
        }
        entry.update(overrides)
        payload = json.dumps({"models": [entry]}).encode("utf-8")
        self.model_catalog_path.write_bytes(payload)
        # Fixtures are pinned exactly like a real run: there is no configuration
        # that reaches `exec` without a digest, so the helper supplies one.
        self.catalog_digest = hashlib.sha256(payload).hexdigest()
        # The child compares what it reads against this, so the end-to-end test
        # is about bytes rather than about the absence of a marker string.
        (self.workspace / "expected-catalog-digest.txt").write_text(
            self.catalog_digest, encoding="utf-8"
        )

    def scenario(self, name: str = "success", **extra: Any) -> None:
        payload: dict[str, Any] = {
            "name": name,
            "pair_id": self.task_input.candidate.pair_id,
            "chain": self.task_input.candidate.chain,
            "observation_ids": [
                str(self.task_input.candidate.price.observation_id),
                str(self.task_input.candidate.liquidity.observation_id),
            ],
        }
        payload.update(extra)
        (self.workspace / "scenario.json").write_text(json.dumps(payload), encoding="utf-8")

    def launcher(self) -> CodexLauncher:
        """A fake, and declared as one.

        The kind is what decides whether a release permit is required, so the
        fixture says what it is rather than borrowing the real form's name.
        Every test here drives this shim; nothing in this suite starts Codex.
        """
        return CodexLauncher(
            kind=LauncherKind.FAKE_EXECUTABLE,
            executable=self.launcher_path,
            path_entries=CHILD_PATH_ENTRIES,
        )

    def config(self, **overrides: Any) -> CodexClientConfig:
        settings: dict[str, Any] = {
            "launcher": self.launcher(),
            "codex_home": self.codex_home,
            "home": self.home,
            "tmpdir": self.tmpdir,
            "workspace": self.workspace,
            "scratch": self.scratch,
            "model": "gpt-5.4",
            "model_catalog_path": self.model_catalog_path,
            "expected_catalog_sha256": self.catalog_digest,
            "effort": "low",
            "run_preflight": False,
        }
        settings.update(overrides)
        return CodexClientConfig(**settings)

    def client(self, **overrides: Any) -> CodexEvaluationClient:
        return CodexEvaluationClient(config=self.config(**overrides))

    def request(self, **overrides: Any) -> EvaluationRequest[OrbitAssessment]:
        settings: dict[str, Any] = {
            "instructions": "Classify the quoted market snapshot. Answer as JSON only.",
            "data": {"candidate": {"pair_id": self.task_input.candidate.pair_id}},
            "output_model": OrbitAssessment,
            "domain_validator": orbit_domain_validator(self.task_input),
            "deadline_seconds": 20.0,
            "cleanup_reserve_seconds": 2.0,
            "limits": OutputLimits(),
        }
        settings.update(overrides)
        return EvaluationRequest(**settings)


@pytest.fixture
def probe(tmp_path: Path) -> Probe:
    return Probe(tmp_path)


@pytest.fixture(autouse=True)
def _inherited_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put credential-shaped variables in the parent so leakage would be visible."""
    for name, value in (
        ("ANTHROPIC_API_KEY", "parent-anthropic"),
        ("OPENAI_API_KEY", "parent-openai"),
        ("CODEX_API_KEY", "parent-codex"),
        ("CODEX_ACCESS_TOKEN", "parent-access"),
        ("CODEX_REFRESH_TOKEN_URL_OVERRIDE", "http://parent.invalid/refresh"),
        ("CODEX_REVOKE_TOKEN_URL_OVERRIDE", "http://parent.invalid/revoke"),
        ("CODEX_APP_SERVER_LOGIN_CLIENT_ID", "parent-client"),
        ("DATABASE_URL", "postgresql+asyncpg://parent/db"),
        ("OPENAI_BASE_URL", "http://parent.invalid/v1"),
    ):
        monkeypatch.setenv(name, value)


__all__ = [
    "CHILD_PATH_ENTRIES",
    "FAKE",
    "LAUNCHER_INJECTED_ENVIRONMENT_KEYS",
    "Probe",
    "orbit_domain_validator",
    "write_launcher",
]
