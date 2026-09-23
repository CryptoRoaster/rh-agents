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
from src.evaluation.codex.auth_home import AUTH_FILE, IsolatedHome
from src.evaluation.codex.catalog import judge_snapshot, snapshot_catalog
from src.evaluation.codex.preflight import VERSION_PATTERN


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


# Answered only by a request, so it can never gate the check that must happen
# before one. Blocking on it would be circular: the only way to clear it is the
# turn it would be blocking.
ADVISORY_GATES = frozenset({"AUTH_REMOTE_VALIDITY"})


@dataclass(frozen=True)
class PreflightStatus:
    """What the harness could establish without contacting a model."""

    gates: tuple[Gate, ...]

    @property
    def may_run(self) -> bool:
        return all(gate.clear for gate in self.gates if gate.name not in ADVISORY_GATES)

    @property
    def blocking(self) -> tuple[Gate, ...]:
        return tuple(
            gate for gate in self.gates if not gate.clear and gate.name not in ADVISORY_GATES
        )

    def render(self) -> str:
        width = max((len(gate.name) for gate in self.gates), default=0)
        lines = [
            f"{gate.name:<{width}}  {gate.state.value:<10}  {gate.detail}" for gate in self.gates
        ]
        verdict = "REAL RUN ALLOWED" if self.may_run else "REAL RUN REFUSED"
        return "\n".join([*lines, "", verdict])


class ReleaseRefused(Exception):
    """A real run was requested without a clear preflight."""

    def __init__(self, blocking: tuple[Gate, ...]) -> None:
        self.blocking = blocking
        names = ", ".join(gate.name for gate in blocking) or "unknown"
        super().__init__(f"release refused: {names}")


_PERMIT = object()


@dataclass(frozen=True)
class ReleaseAuthorization:
    """Evidence that every blocking gate passed, and the only way to hold it.

    Deliberately not a boolean and not a mode. `RunMode` was removed for
    exactly that reason: a flag the caller sets is a promise, not a check. This
    can only come from `authorize`, which produces one solely from a status
    where nothing is blocking, and the real runner requires an instance rather
    than a claim.
    """

    status: PreflightStatus
    _permit: object

    def __post_init__(self) -> None:
        if self._permit is not _PERMIT:
            raise ValueError("a ReleaseAuthorization may only come from authorize()")


def authorize(status: PreflightStatus) -> ReleaseAuthorization:
    """Turn a clear preflight into an authorization, or refuse."""
    if not status.may_run:
        raise ReleaseRefused(status.blocking)
    return ReleaseAuthorization(status=status, _permit=_PERMIT)


GATE_NAMES = (
    "MODEL_CATALOG",
    "CATALOG_DIGEST",
    "CODEX_VERSION",
    "CHATGPT_SESSION",
    "TOOL_SURFACE",
    "OUTER_READ_SANDBOX",
    "NETWORK_EGRESS",
    "AUTH_HOME_ISOLATION",
    "AUTH_REMOTE_VALIDITY",
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
        _remote_validity_gate(),
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
        # The catalog was readable, within bounds and strictly decodable. What
        # it holds is TOOL_SURFACE's question, and this detail does not claim
        # to have answered it.
        Gate("MODEL_CATALOG", GateState.PASS, f"snapshot taken, {taken.digest[:12]}"),
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
    """Compare the parsed version, not a substring.

    `supported in cli_version` would accept "0.153.40" for "0.153.4", which is a
    different build with a different contract. The same extraction the client
    uses decides here.
    """
    if cli_version is None:
        return [Gate("CODEX_VERSION", GateState.UNVERIFIED, "build not identified")]
    found = VERSION_PATTERN.search(cli_version)
    parsed = found.group(1) if found is not None else None
    if parsed is None:
        return [Gate("CODEX_VERSION", GateState.FAIL, f"no version in {cli_version!r}")]
    matches = parsed == supported
    return [
        Gate(
            "CODEX_VERSION",
            GateState.PASS if matches else GateState.FAIL,
            parsed if matches else f"{parsed}, expected {supported}",
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
        return [
            Gate("OUTER_READ_SANDBOX", GateState.FAIL, "sandbox-exec or profile missing"),
            Gate("NETWORK_EGRESS", GateState.FAIL, "no outer sandbox to measure"),
        ]
    if roots is None or probe_outside is None:
        return [
            Gate("OUTER_READ_SANDBOX", GateState.FAIL, "no outer sandbox configured"),
            Gate("NETWORK_EGRESS", GateState.FAIL, "no outer sandbox to measure"),
        ]

    profile = sandbox.write_profile(probe_outside)
    try:
        result = sandbox.probe_boundaries(roots, profile, probe_outside)
    finally:
        profile.unlink(missing_ok=True)

    gates: list[Gate] = []
    if result.read_boundary_holds and result.write_scope_holds:
        gates.append(
            Gate(
                "OUTER_READ_SANDBOX",
                GateState.PASS,
                "workspace readable, outside denied, one writable auth file",
            )
        )
    else:
        gates.append(
            Gate(
                "OUTER_READ_SANDBOX",
                GateState.FAIL,
                f"allowed={result.allowed_readable} forbidden={result.forbidden_readable}"
                f" sentinel={result.sentinel_readable} auth_rw="
                f"{result.auth_readable and result.auth_rewritable}"
                f" beside={result.other_file_creatable}",
            )
        )
    gates.append(
        Gate(
            "NETWORK_EGRESS",
            GateState.PASS if result.egress_reachable else GateState.FAIL,
            "outbound tcp reaches a loopback listener"
            if result.egress_reachable
            else "outbound tcp blocked; the provider would be unreachable",
        )
    )
    return gates


def _home_gate(isolated_home: IsolatedHome | None) -> list[Gate]:
    """Judge the isolation itself, not whether the token will be accepted.

    Those were conflated before, and the result was a gate nothing could ever
    clear: `may_run` demanded every gate pass, while this one could only pass
    after the turn it was blocking. The isolation is checkable here and now --
    the directory exists, holds exactly the expected file, and has restrictive
    modes. Whether the server accepts the token is a different question, kept
    separate under AUTH_REMOTE_VALIDITY.
    """
    if isolated_home is None:
        return [Gate("AUTH_HOME_ISOLATION", GateState.FAIL, "no isolated CODEX_HOME")]
    if not isolated_home.auth_present:
        return [Gate("AUTH_HOME_ISOLATION", GateState.FAIL, "no login state copied")]

    problem = _isolation_problem(isolated_home.path)
    if problem is not None:
        return [Gate("AUTH_HOME_ISOLATION", GateState.FAIL, problem)]
    return [
        Gate(
            "AUTH_HOME_ISOLATION",
            GateState.PASS,
            "0700 home holding only a 0600 auth file",
        )
    ]


def _isolation_problem(home: Path) -> str | None:
    """Check the shape of the isolated home without reading what is in it."""
    try:
        if home.stat().st_mode & 0o777 != 0o700:
            return "home is not 0700"
        names = sorted(item.name for item in home.iterdir())
        if names != [AUTH_FILE]:
            return f"home holds {len(names)} entries, expected only the auth file"
        if (home / AUTH_FILE).stat().st_mode & 0o777 != 0o600:
            return "auth file is not 0600"
    except OSError as error:
        return type(error).__name__
    return None


def _remote_validity_gate() -> list[Gate]:
    """Unknowable before a request, and therefore never a blocker.

    A local check can show that Codex recognises the copied login state. It
    cannot show that the provider will accept the token on the next request,
    and no amount of offline work changes that. This is an operability
    uncertainty, not an open security boundary.
    """
    return [
        Gate(
            "AUTH_REMOTE_VALIDITY",
            GateState.UNVERIFIED,
            "server acceptance is unknowable without a request; advisory only",
        )
    ]


__all__ = [
    "ADVISORY_GATES",
    "GATE_NAMES",
    "Gate",
    "GateState",
    "PreflightStatus",
    "ReleaseAuthorization",
    "ReleaseRefused",
    "authorize",
    "evaluate_release",
]
