"""The gates a real turn would have to pass, evaluated offline.

Every gate here is either PASS, FAIL or UNVERIFIED, and `may_run` is true only
when none of them is anything but PASS. There is no "warning but continue":
a gate that cannot be established is a gate that did not pass, and
`OUTER_READ_SANDBOX` and `CATALOG_DIGEST` in particular have no degraded mode.

UNVERIFIED is its own answer on purpose. Some things cannot be settled without
a real turn -- whether the copied login state actually authenticates, above all
-- and calling that PASS because the mechanism exists would be the same mistake
as calling a quiet run proof that no tool was offered.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from src.evaluation.codex import sandbox
from src.evaluation.codex.auth_home import IsolatedHome
from src.evaluation.codex.catalog import judge_snapshot, snapshot_catalog


class GateState(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNVERIFIED = "UNVERIFIED"


@dataclass(frozen=True)
class Gate:
    name: str
    state: GateState
    detail: str

    @property
    def clear(self) -> bool:
        return self.state is GateState.PASS


@dataclass(frozen=True)
class PreflightStatus:
    """What the harness could establish without contacting a model."""

    gates: tuple[Gate, ...]

    @property
    def may_run(self) -> bool:
        return all(gate.clear for gate in self.gates)

    @property
    def blocking(self) -> tuple[Gate, ...]:
        return tuple(gate for gate in self.gates if not gate.clear)

    def render(self) -> str:
        width = max((len(gate.name) for gate in self.gates), default=0)
        lines = [
            f"{gate.name:<{width}}  {gate.state.value:<10}  {gate.detail}" for gate in self.gates
        ]
        verdict = "REAL RUN ALLOWED" if self.may_run else "REAL RUN REFUSED"
        return "\n".join([*lines, "", verdict])


GATE_NAMES = (
    "MODEL_CATALOG",
    "CATALOG_DIGEST",
    "CODEX_VERSION",
    "CHATGPT_SESSION",
    "TOOL_SURFACE",
    "OUTER_READ_SANDBOX",
    "AUTH_HOME_ISOLATION",
)


def evaluate_release(
    *,
    catalog_path: Path,
    expected_digest: str,
    model: str,
    snapshot_dir: Path,
    cli_version: str | None,
    supported_version: str,
    chatgpt_session: bool | None,
    roots: sandbox.SandboxRoots | None,
    probe_outside: Path | None,
    isolated_home: IsolatedHome | None,
) -> PreflightStatus:
    """Work out, without contacting a model, whether a real turn could proceed."""
    gates = [
        _catalog_gates(catalog_path, expected_digest, model, snapshot_dir),
        _version_gate(cli_version, supported_version),
        _session_gate(chatgpt_session),
        _sandbox_gate(roots, probe_outside),
        _home_gate(isolated_home),
    ]
    flattened: list[Gate] = []
    for item in gates:
        flattened.extend(item)
    return PreflightStatus(gates=tuple(flattened))


def _catalog_gates(
    catalog_path: Path, expected_digest: str, model: str, snapshot_dir: Path
) -> list[Gate]:
    taken = snapshot_catalog(catalog_path, snapshot_dir)
    if taken is None:
        unreadable = "catalog could not be read, bounded, decoded or snapshotted"
        return [
            Gate("MODEL_CATALOG", GateState.FAIL, unreadable),
            Gate("CATALOG_DIGEST", GateState.FAIL, unreadable),
            Gate("TOOL_SURFACE", GateState.FAIL, unreadable),
        ]
    try:
        digest_ok = taken.digest == expected_digest
        verdict = judge_snapshot(taken, model, expected_digest)
    finally:
        taken.close()

    return [
        Gate("MODEL_CATALOG", GateState.PASS, f"one entry for {model}"),
        Gate(
            "CATALOG_DIGEST",
            GateState.PASS if digest_ok else GateState.FAIL,
            "pinned digest matches" if digest_ok else "pinned digest does not match",
        ),
        Gate(
            "TOOL_SURFACE",
            GateState.PASS if verdict.reason is None else GateState.FAIL,
            "within the allowlist" if verdict.reason is None else str(verdict.reason),
        ),
    ]


def _version_gate(cli_version: str | None, supported: str) -> list[Gate]:
    if cli_version is None:
        return [Gate("CODEX_VERSION", GateState.UNVERIFIED, "build not identified")]
    matches = supported in cli_version
    return [
        Gate(
            "CODEX_VERSION",
            GateState.PASS if matches else GateState.FAIL,
            cli_version if matches else f"{cli_version}, expected {supported}",
        )
    ]


def _session_gate(chatgpt_session: bool | None) -> list[Gate]:
    if chatgpt_session is None:
        # Not probed. Saying PASS because a login exists somewhere would be a
        # guess about the home the attempt will actually use.
        return [Gate("CHATGPT_SESSION", GateState.UNVERIFIED, "login not probed")]
    return [
        Gate(
            "CHATGPT_SESSION",
            GateState.PASS if chatgpt_session else GateState.FAIL,
            "ChatGPT session reported" if chatgpt_session else "no ChatGPT session",
        )
    ]


def _sandbox_gate(roots: sandbox.SandboxRoots | None, probe_outside: Path | None) -> list[Gate]:
    if not sandbox.macos():
        # Linux would need bwrap or landlock, which is not implemented. Refusing
        # is the only honest answer; there is no degraded mode here.
        return [Gate("OUTER_READ_SANDBOX", GateState.FAIL, "only implemented for macOS")]
    if not sandbox.available():
        return [Gate("OUTER_READ_SANDBOX", GateState.FAIL, "sandbox-exec or profile missing")]
    if roots is None or probe_outside is None:
        return [Gate("OUTER_READ_SANDBOX", GateState.FAIL, "no outer sandbox configured")]

    profile = sandbox.write_profile(probe_outside)
    try:
        result = sandbox.probe_read_boundary(roots, profile, probe_outside)
    finally:
        profile.unlink(missing_ok=True)

    if result.boundary_holds:
        return [
            Gate(
                "OUTER_READ_SANDBOX",
                GateState.PASS,
                "workspace readable, outside paths denied",
            )
        ]
    return [
        Gate(
            "OUTER_READ_SANDBOX",
            GateState.FAIL,
            f"allowed={result.allowed_readable} forbidden={result.forbidden_readable}"
            f" sentinel={result.sentinel_readable}",
        )
    ]


def _home_gate(isolated_home: IsolatedHome | None) -> list[Gate]:
    if isolated_home is None:
        return [Gate("AUTH_HOME_ISOLATION", GateState.FAIL, "no isolated CODEX_HOME")]
    if not isolated_home.auth_present:
        return [Gate("AUTH_HOME_ISOLATION", GateState.FAIL, "no login state copied")]
    # The mechanism is in place and the file is there. Whether that state
    # actually authenticates cannot be settled without a real turn, so this is
    # deliberately not a PASS.
    return [
        Gate(
            "AUTH_HOME_ISOLATION",
            GateState.UNVERIFIED,
            "isolated home holds the login state; sufficiency needs a real turn",
        )
    ]


__all__ = [
    "GATE_NAMES",
    "Gate",
    "GateState",
    "PreflightStatus",
    "evaluate_release",
]
