"""The gates a real turn would have to pass, and the binding it runs under.

Every gate is PASS, FAIL or UNVERIFIED, and there is no "warning but continue":
a gate that cannot be established is a gate that did not pass. UNVERIFIED is its
own answer on purpose -- whether the copied login state will be accepted by the
provider cannot be settled offline, and calling it PASS because the mechanism
exists would be the same mistake as calling a quiet run proof that no tool was
offered.

Two things this module is careful about, both of which it previously got wrong.

**The gate set is fixed.** `all(...)` over an empty tuple is `True`, so
`PreflightStatus(gates=())` used to authorise a real run, and so did a single
invented `Gate("EVERYTHING", PASS, ...)`. `authorize` now checks the shape of
the status before its contents: exactly the required names, each exactly once,
nothing else. A gate set that is merely *not failing* is not a preflight. The
same requirement is why `evaluate_release` emits every gate on every platform --
on Linux the sandbox gates are FAIL rather than absent.

**The authorization carries what was checked.** A cleared preflight says
something about one concrete configuration: this launcher, this catalog, this
workspace, this isolated home, these sandbox roots, this profile. An
authorization that did not carry those could be paired with a different
configuration afterwards, and the green preflight would be about something other
than what runs.

What this is not: `ReleaseAuthorization` is not unforgeable. Module privacy in
Python is a convention, not a security boundary, and an in-process caller that
sets out to build one can. The property being bought is fail-closed behaviour
against miswiring and accidental bypass -- a configuration that was never
checked, or that drifted after it was, does not run.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from src.evaluation.codex import sandbox
from src.evaluation.codex.auth_home import (
    AUTH_FILE,
    AUTH_MODE,
    EXPECTED_ENTRIES,
    INSTALLATION_ID_FILE,
    INSTALLATION_ID_MODE,
    IsolatedHome,
)
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

# The shape a preflight must have before its contents are worth reading. Every
# name appears exactly once, nothing else appears at all, and this is checked
# first -- an empty tuple and a single invented gate both used to satisfy the
# contents check, because `all(...)` over nothing is true.
REQUIRED_GATES = (
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

# Every advisory gate must also be a required gate, or the exemption would name
# something the status is not obliged to contain.
assert ADVISORY_GATES <= frozenset(REQUIRED_GATES)


@dataclass(frozen=True)
class RunBinding:
    """The security-relevant identity of the configuration a preflight cleared.

    Plain strings rather than `Path` objects, and every path already resolved:
    equality then means "the same real location", not "a path that happens to
    spell the same way". `/tmp` and `/private/tmp` are the standing example of
    why that distinction is not academic.

    What belongs here is anything that changes what the run may reach. Not the
    reasoning effort, which changes what the model does rather than what it can
    touch; but the exec budget does belong, because "at most one attempt" is a
    property of the release and not of the caller's intentions.
    """

    model: str
    catalog_digest: str
    catalog_path: str
    # The named runtime copy: where it is, and what is in it. Both, because a
    # path alone authorises whatever that path holds at exec time and a digest
    # alone authorises those bytes wherever they are reached from.
    runtime_catalog_path: str
    runtime_catalog_digest: str
    launcher_kind: str
    launcher_path: str
    supported_cli_version: str
    workspace: str
    codex_home: str
    home: str
    tmpdir: str
    scratch: str
    sandbox_vendor: str
    sandbox_workspace: str
    sandbox_codex_home: str
    sandbox_auth_file: str
    sandbox_installation_id_file: str
    sandbox_catalog_file: str
    forbidden_roots: tuple[str, ...]
    run_preflight: bool
    max_exec_starts: int
    profile_sha256: str

    def differences(self, other: "RunBinding") -> tuple[str, ...]:
        """Which fields disagree, so a refusal can say what drifted."""
        return tuple(
            name
            for name in self.__dataclass_fields__
            if getattr(self, name) != getattr(other, name)
        )


# Minted only by `ReleaseAuthorization.permit`. Same convention as `_PERMIT`
# below, and the same caveat: this is not a security boundary against an
# in-process caller who sets out to defeat it. It is what makes the accident --
# building a permit beside the preflight rather than out of one -- fail.
_RELEASE_PERMIT_TOKEN = object()


@dataclass(frozen=True)
class ReleasePermit:
    """What a process invocation against a real Codex build has to be handed.

    The client is the offline engine and stays usable with a fake launcher and
    no permit at all. What it will not do is start a *real* build without one,
    so a configuration that never went through a preflight cannot reach `exec`
    by being constructed directly.

    Carrying a matching `RunBinding` is necessary but was not sufficient on its
    own: a caller holding a configuration can compute `binding_for(config, ...)`
    from it and hand the client a permit that agrees with itself, which says
    nothing about a preflight ever having run. So the binding has to have come
    *through* an authorization, and the token is how that is expressed.

    Not unforgeable -- module privacy in Python is a convention. Fail-closed
    against miswiring, which is the property being bought.
    """

    binding: RunBinding
    # Defaulted so that `ReleasePermit(binding=...)` is refused here with a
    # reason rather than by an argument-count error further out.
    _token: object | None = None

    def __post_init__(self) -> None:
        if self._token is not _RELEASE_PERMIT_TOKEN:
            raise ValueError("a ReleasePermit may only come from ReleaseAuthorization.permit()")


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
    """A real run was requested without a clear preflight.

    Carries both halves of the answer: which required gates did not pass, and
    what was wrong with the shape of the status itself. A status that is the
    wrong shape is refused before its contents are read, so the two are
    reported separately rather than folded into one list.
    """

    def __init__(self, blocking: tuple[Gate, ...], problems: tuple[str, ...] = ()) -> None:
        self.blocking = blocking
        self.problems = problems
        detail = ", ".join([*problems, *(gate.name for gate in blocking)]) or "unknown"
        super().__init__(f"release refused: {detail}")


_PERMIT = object()


@dataclass(frozen=True)
class ReleaseAuthorization:
    """A cleared preflight together with the configuration it cleared.

    Not a boolean and not a mode. `RunMode` was removed because a flag the
    caller sets is a promise rather than a check, and a second boolean would
    have been the same mistake under a new name. This comes from `authorize`,
    which makes one only from a status of the required shape where nothing
    blocking failed, and it carries a `RunBinding` so the configuration that
    runs can be compared against the configuration that was checked.

    It is *not* unforgeable. `_PERMIT` is module-private by convention, and
    convention is not a security boundary in Python. What this buys is
    fail-closed behaviour against miswiring: a run that was never cleared, or
    whose configuration drifted after it was, does not start.
    """

    status: PreflightStatus
    binding: RunBinding
    _permit: object

    def __post_init__(self) -> None:
        if self._permit is not _PERMIT:
            raise ValueError("a ReleaseAuthorization may only come from authorize()")

    def permit(self) -> ReleasePermit:
        """The token a client needs before it may start a real build.

        The only place `_RELEASE_PERMIT_TOKEN` is used, so a permit exists only
        where an authorization does, and an authorization exists only where a
        preflight of the required shape cleared.
        """
        return ReleasePermit(binding=self.binding, _token=_RELEASE_PERMIT_TOKEN)


def shape_problems(status: PreflightStatus) -> tuple[str, ...]:
    """What is wrong with the *set* of gates, before any state is considered.

    Three separate ways a status can fail to be a preflight at all: a required
    gate is missing, a required gate appears more than once, or a gate nobody
    asked for is present. Each is reported by name, because "release refused"
    with no reason is how a caller ends up removing the check.
    """
    seen = [gate.name for gate in status.gates]
    problems: list[str] = []
    for name in REQUIRED_GATES:
        count = seen.count(name)
        if count == 0:
            problems.append(f"missing gate {name}")
        elif count > 1:
            problems.append(f"duplicate gate {name}")
    for name in sorted(set(seen) - set(REQUIRED_GATES)):
        problems.append(f"unexpected gate {name}")
    return tuple(problems)


def authorize(status: PreflightStatus, binding: RunBinding) -> ReleaseAuthorization:
    """Turn a clear preflight over the required gate set into an authorization."""
    problems = shape_problems(status)
    if problems or not status.may_run:
        raise ReleaseRefused(status.blocking, problems)
    return ReleaseAuthorization(status=status, binding=binding, _permit=_PERMIT)


# Kept as the published name for the gate set; `REQUIRED_GATES` is what
# `authorize` enforces, and they are deliberately the same tuple.
GATE_NAMES = REQUIRED_GATES


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


def _unmeasurable(reason: str) -> list[Gate]:
    """Both sandbox gates, failed for the same reason.

    Always *both*. An absent gate is not a safe absence: `authorize` requires
    the full gate set, and a status that silently drops one on a platform where
    it cannot be measured would be refused for the wrong reason -- or, before
    the shape check existed, accepted for no reason at all. This is also what
    made the Linux CI run disagree with the macOS one.
    """
    return [
        Gate("OUTER_READ_SANDBOX", GateState.FAIL, reason),
        Gate("NETWORK_EGRESS", GateState.FAIL, reason),
    ]


def _sandbox_gate(roots: sandbox.SandboxRoots | None, probe_outside: Path | None) -> list[Gate]:
    if not sandbox.macos():
        # Linux would need bwrap or landlock, which is not implemented. Refusing
        # is the only honest answer; there is no degraded mode here.
        return _unmeasurable("only implemented for macOS")
    if not sandbox.available():
        return _unmeasurable("sandbox-exec or profile missing")
    if roots is None or probe_outside is None:
        return _unmeasurable("no outer sandbox configured")

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
                "workspace readable, outside denied, two writable files,"
                " catalog re-readable and unmodifiable",
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
                f" beside={result.other_file_creatable}"
                f" catalog_replayable={result.catalog_replayable}"
                f" catalog_locked={not result.catalog_writable}",
            )
        )
    # Both halves, because one of them alone was green while the turn could
    # not reach anything. The sixth real probe got past `turn.started` and then
    # failed on `failed to lookup address information`, while this gate
    # reported PASS -- it measured a TCP connection to `127.0.0.1`, which needs
    # no name resolved. Reaching an address and being able to find one are two
    # permissions, and the profile granted only the first.
    reachable = result.egress_reachable and result.name_resolution_available
    gates.append(
        Gate(
            "NETWORK_EGRESS",
            GateState.PASS if reachable else GateState.FAIL,
            "outbound tcp reaches a loopback listener and the resolver is reachable"
            if reachable
            else f"tcp={result.egress_reachable} resolver={result.name_resolution_available};"
            " the provider would be unreachable",
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
            "0700 home holding only 0600 auth.json and 0644 installation_id",
        )
    ]


def _isolation_problem(home: Path) -> str | None:
    """Check the shape of the isolated home without reading what is in it.

    Two files, not one. `codex exec` starts an in-process app-server client
    whose `resolve_installation_id` requires `CODEX_HOME/installation_id`, so
    "exactly one entry" was the wrong shape for 0.153.4 -- the second real
    probe died on it. The set stays closed: a third entry is still a failure,
    and both modes are still checked, because the point of the gate is that
    the file set is known in advance rather than whatever the CLI left behind.
    """
    try:
        if home.stat().st_mode & 0o777 != 0o700:
            return "home is not 0700"
        names = sorted(item.name for item in home.iterdir())
        if names != sorted(EXPECTED_ENTRIES):
            expected = ", ".join(sorted(EXPECTED_ENTRIES))
            return f"home holds {len(names)} entries, expected exactly {expected}"
        for name, mode in (
            (AUTH_FILE, AUTH_MODE),
            (INSTALLATION_ID_FILE, INSTALLATION_ID_MODE),
        ):
            if (home / name).stat().st_mode & 0o777 != mode:
                return f"{name} is not {mode:04o}"
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
    "REQUIRED_GATES",
    "Gate",
    "GateState",
    "PreflightStatus",
    "ReleaseAuthorization",
    "ReleasePermit",
    "ReleaseRefused",
    "RunBinding",
    "authorize",
    "evaluate_release",
    "shape_problems",
]
