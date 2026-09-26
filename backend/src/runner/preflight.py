"""What this installation could do, asked without doing any of it.

`python -m src.runner.main --preflight` answers one question: **can the
configuration in front of me build the production stack a bounded PAPER run
needs, and what is missing?** It is a separate mode from `--once` and cannot be
combined with it, because the two make opposite promises — one changes nothing
and one is allowed to trade.

What it does *not* do, and why that is the point
------------------------------------------------

**It performs no run.** No intake cycle, no worker registration, no claim, no
risk request, no fill, no recording, no migration — nothing that writes to the
database at all. The only database work is reading two facts, each bounded by
the same timeout a run puts on one of its own steps.

**It calls nobody.** No provider, RPC endpoint, indexer or model is contacted,
and no constructor here reaches one on the quiet: composing the stack builds
clients, and building an HTTP or RPC client opens no connection. What that costs
is precision, and the report says so rather than hiding it — a configured key is
reported as *configured*, never as valid, and a selected provider as *selected*,
never as reachable.

**It is not an authorization.** A green preflight says the machinery could be
assembled a moment ago. It is not consent to trade, it does not survive a
change of configuration, and it replaces nothing: the executing CLI still runs
its own refusals, SENTINEL still judges every request, the freshness and
execution bounds still apply, and a stop committed one second later still stops
the run. A run refuses for reasons a preflight cannot see, and that is correct.

Three answers, kept apart
-------------------------

*Satisfied* — asked locally and met. *Blocked* — asked locally and standing in
the way. *Not checked* — the honest answer where finding out would mean calling
somebody, and a claim would be a guess dressed as a fact. A fourth, *unavailable*,
is for a check this process could not carry out at all: the database did not
answer in time, or answered with something this code cannot interpret. That is a
fault in the check, not a verdict about the configuration, and it exits
accordingly.

Every check reuses a contract that already exists — `refuse()`, `build_stack`,
`RunnerStack.misconfigured`, `selected_chains`, the limit builders, the schema
revision the migrations declare, and the same durable pause reader the control
plane uses. Nothing here is a second opinion about what a run requires; it is
the same objects, asked instead of used.
"""

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.core.config import Settings
from src.data.database import connect
from src.data.schema import SchemaUnknown, expected_revision, is_current, recorded_revisions
from src.markets.geckoterminal.errors import ProviderError
from src.markets.geckoterminal.networks import selected_chains
from src.orchestration.commander.context import AccountPauseReader, SystemPauseUnavailable
from src.runner.composition import (
    RunnerPorts,
    RunnerStack,
    acquisition_limits_from_settings,
    runner_stack,
)
from src.runner.models import Code, ExitCode, Immutable, RoleAvailability
from src.runner.service import refuse

# A short human sentence beside the code. Printable ASCII only and authored
# here: the values that reach it are counts, role names, chain names and
# revision identifiers, never a configured value and never an exception text.
Note = Annotated[str, Field(max_length=200, pattern=r"^[ -~]*$")]


class CheckStatus(StrEnum):
    """What this process actually established about one precondition."""

    # Asked locally, and met.
    SATISFIED = "SATISFIED"
    # Asked locally, and in the way. A run would refuse, or would open work
    # nobody could finish.
    BLOCKED = "BLOCKED"
    # Not asked, because answering would mean calling somebody. Never a
    # complaint and never an approval — an absence of knowledge, stated.
    NOT_CHECKED = "NOT_CHECKED"
    # Could not be carried out. A fault in the check itself, not a verdict
    # about the configuration.
    UNAVAILABLE = "UNAVAILABLE"


class Check(Immutable):
    """One precondition, what became of asking about it, and why."""

    name: Code
    # A `CheckStatus` value.
    status: Code
    # This system's own code for the outcome, where there is one to give.
    reason: Code | None = None
    note: Note = ""


class PreflightReading(Immutable):
    """One structured account of what could be established locally.

    Safe to print anywhere by construction: every field is a code this system
    already publishes, a count, or a sentence written in this file. No
    configured value, credential, URL or exception text is representable here,
    so none can leak through it.
    """

    kind: Literal["paper_run_preflight"] = "paper_run_preflight"
    checked_at: str
    # True only when nothing was found in the way and every check ran. Says
    # nothing whatever about reachability, validity or a future run.
    ready: bool = Field(strict=True)
    checks: tuple[Check, ...] = Field(default=(), max_length=64)
    roles: tuple[RoleAvailability, ...] = Field(default=(), max_length=16)
    # Checks this process could not carry out. Their presence is what makes the
    # difference between "not ready" and "cannot tell".
    errors: tuple[Code, ...] = Field(default=(), max_length=16)

    @property
    def blocked(self) -> tuple[Check, ...]:
        return tuple(item for item in self.checks if item.status == CheckStatus.BLOCKED.value)

    @property
    def exit_code(self) -> ExitCode:
        """The same three-value contract the executing CLI publishes.

        `0` everything this process could check is in place. `2` something is
        missing, and it is a configuration statement rather than an outage.
        `1` a check could not be carried out, which is the one code that means
        somebody should go and look at the machine.
        """
        if self.errors:
            return ExitCode.TECHNICAL_FAILURE
        if self.blocked:
            return ExitCode.CONFIGURATION_REFUSED
        return ExitCode.COMPLETED


def _check(name: str, status: CheckStatus, *, reason: str | None = None, note: str = "") -> Check:
    return Check(name=name, status=status.value, reason=reason, note=note)


def _satisfied(name: str, note: str = "") -> Check:
    return _check(name, CheckStatus.SATISFIED, note=note)


def _blocked(name: str, reason: str, note: str = "") -> Check:
    return _check(name, CheckStatus.BLOCKED, reason=reason, note=note)


def _external(name: str, note: str, reason: str = "REQUIRES_EXTERNAL_CALL") -> Check:
    return _check(name, CheckStatus.NOT_CHECKED, reason=reason, note=note)


class Preflight:
    """Every locally answerable question about one configuration.

    Owns nothing and changes nothing. The stack it composes is released through
    the same context manager a run uses, so the clients it builds — and never
    calls — are closed again on every path.
    """

    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Clock | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._clock = clock if clock is not None else SystemClock()
        # The bound a run already puts on one external wait. Reused rather than
        # configured again: a check that may hang is not a check, and a second
        # knob for the same idea is a second thing to get wrong.
        self._timeout = float(settings.paper_runner_step_timeout_seconds)

    async def run(self, stack: RunnerStack) -> PreflightReading:
        checks: list[Check] = []
        errors: list[str] = []
        checks.append(self._permitted())
        checks.append(self._chains())
        checks.extend(self._budgets(stack))
        checks.extend(self._roles(stack))
        checks.extend(self._scout(stack))
        checks.append(await self._schema(errors))
        checks.append(await self._pause(errors))
        checks.extend(self._unverifiable())
        blocked = any(item.status == CheckStatus.BLOCKED.value for item in checks)
        return PreflightReading(
            checked_at=self._clock.now().isoformat(),
            ready=not blocked and not errors,
            checks=tuple(checks),
            roles=stack.roles,
            errors=tuple(dict.fromkeys(errors)),
        )

    # ------------------------------------------------------------ configuration

    def _permitted(self) -> Check:
        """Whether a run may start at all, asked of the function that decides.

        `refuse()` is the executing CLI's own gate, called here rather than
        restated. It answers with the first reason a run would be refused, which
        is exactly what an operator needs to remove.
        """
        refusal = refuse(self._settings)
        if refusal is None:
            return _satisfied(
                "RUN_PERMITTED", "The mode, the runner switch and the kill switch all allow a run."
            )
        return _blocked(
            "RUN_PERMITTED",
            refusal.reason,
            "A run would be refused before it could write anything.",
        )

    def _chains(self) -> Check:
        """The configured chains, judged by the directory's own rule."""
        try:
            chains = selected_chains(self._settings)
        except ProviderError as error:
            return _blocked(
                "MARKET_CHAINS",
                error.code.upper(),
                "More chains are named than this provider configuration permits.",
            )
        names = ", ".join(item.name for item in chains)
        return _satisfied("MARKET_CHAINS", f"Configured chains: {names}.")

    def _budgets(self, stack: RunnerStack) -> list[Check]:
        """The bounds a run would hold itself to, built the way a run builds them.

        Constructing them *is* the check: every limit is a validated contract
        and their dependencies — a step that may not outlast the run, an
        acquisition that may not outlast it either — are settings validators
        that have already refused an impossible combination before this point.
        """
        limits = stack.limits
        found: list[Check] = [
            _satisfied(
                "RUN_BUDGETS",
                f"candidates {limits.max_candidates}, new cases {limits.max_new_cases}, "
                f"cases {limits.max_cases}, steps {limits.max_steps}, "
                f"runtime {limits.max_runtime_seconds}s, step {limits.step_timeout_seconds}s.",
            )
        ]
        if not self._settings.paper_runner_market_acquisition_enabled:
            found.append(
                _satisfied(
                    "MARKET_ACQUISITION",
                    "Not enabled. A run would trade only what is already recorded.",
                )
            )
            return found
        acquisition = acquisition_limits_from_settings(self._settings)
        if stack.acquisition is None:
            # Enabled and not composed is a contradiction this configuration
            # produced, and reporting it as merely absent would read like a
            # deliberate choice.
            found.append(
                _blocked(
                    "MARKET_ACQUISITION",
                    "ACQUISITION_NOT_COMPOSED",
                    "Acquisition is enabled but the stack did not compose it.",
                )
            )
            return found
        found.append(
            _satisfied(
                "MARKET_ACQUISITION",
                f"markets {acquisition.max_markets}, discovery reads "
                f"{acquisition.max_discovery_requests}, provider requests "
                f"{acquisition.max_provider_requests}, attempts "
                f"{acquisition.max_http_attempts}, {acquisition.max_seconds}s.",
            )
        )
        return found

    def _roles(self, stack: RunnerStack) -> list[Check]:
        """Every specialist, as the composition itself reports it.

        Three outcomes and they are not the same. A role that composed is ready.
        A role somebody switched off is a decision, and reporting it as a
        problem would teach an operator to ignore the list. A role that is on
        and could not be built is the mistake this whole mode exists to surface,
        and it is the same set `RunnerStack.misconfigured` refuses a run over.
        """
        broken = {item.role for item in stack.misconfigured}
        found: list[Check] = []
        for role in stack.roles:
            name = f"ROLE_{role.role}"
            if role.available:
                found.append(_satisfied(name, "Enabled and composable."))
            elif role.role in broken:
                found.append(
                    _blocked(
                        name,
                        role.reason or "ROLE_NOT_CONFIGURED",
                        "Enabled, and this configuration cannot build what it needs.",
                    )
                )
            else:
                found.append(_satisfied(name, "Deliberately not enabled."))
        return found

    def _scout(self, stack: RunnerStack) -> list[Check]:
        """The early-discovery scout, only when it is switched on.

        Reported beside the run's own checks rather than folded into them: the
        scout holds no trading authority, so nothing here asks for PAPER mode,
        and nothing here makes a run more or less permitted than it was.
        """
        settings = self._settings
        if not settings.early_scout_enabled:
            return []
        found: list[Check] = []
        if settings.market_provider == "geckoterminal":
            found.append(
                _satisfied("EARLY_SCOUT_MARKET_PROVIDER", "New pools come from GeckoTerminal.")
            )
        else:
            found.append(
                _blocked(
                    "EARLY_SCOUT_MARKET_PROVIDER",
                    "EARLY_SCOUT_PROVIDER_NOT_GECKOTERMINAL",
                    "The scout discovers and re-observes through GeckoTerminal only.",
                )
            )
        if stack.reasoning_unavailable is None:
            found.append(
                _satisfied(
                    "EARLY_SCOUT_REASONING", "ORBIT's model is configured; nothing is called."
                )
            )
        else:
            found.append(
                _blocked(
                    "EARLY_SCOUT_REASONING",
                    stack.reasoning_unavailable,
                    "Scout reviews use ORBIT, and no model is configured for it.",
                )
            )
        found.append(
            _satisfied(
                "EARLY_SCOUT_BUDGETS",
                f"discovery pools {settings.early_scout_max_discovery_pools}, new watches "
                f"{settings.early_scout_max_new_watches_per_run}, reviews "
                f"{settings.early_scout_max_orbit_reviews_per_run}, history checks "
                f"{settings.early_scout_max_history_checks_per_run}, refreshes "
                f"{settings.early_scout_max_refresh_markets_per_run}.",
            )
        )
        return found

    # ---------------------------------------------------------------- database

    async def _schema(self, errors: list[str]) -> Check:
        """What the database was migrated to, against what this code expects.

        The expectation comes from the migration chain that ships with the code,
        never from a constant: a readiness check comparing against a revision
        nobody has shipped for months passes for the wrong reason, and is
        trusted while doing it.

        The comparison is over the whole recorded set. A database is the one
        this code expects only when what it records is exactly the one head —
        no extra revision is ignored, and no row is selected to produce a match.
        """
        try:
            expected = expected_revision()
        except SchemaUnknown:
            errors.append("SCHEMA_EXPECTATION_UNKNOWN")
            return _check(
                "DATABASE_SCHEMA",
                CheckStatus.UNAVAILABLE,
                reason="SCHEMA_EXPECTATION_UNKNOWN",
                note="The migration chain beside this code could not be read.",
            )
        try:
            found = await self._bounded(self._read_revisions())
        except TimeoutError:
            errors.append("DATABASE_TIMEOUT")
            return _check(
                "DATABASE_SCHEMA",
                CheckStatus.UNAVAILABLE,
                reason="DATABASE_TIMEOUT",
                note="The database did not answer within the configured step bound.",
            )
        except (SQLAlchemyError, OSError):
            errors.append("DATABASE_UNAVAILABLE")
            return _check(
                "DATABASE_SCHEMA",
                CheckStatus.UNAVAILABLE,
                reason="DATABASE_UNAVAILABLE",
                note="The database could not be read.",
            )
        if not found:
            return _blocked(
                "DATABASE_SCHEMA",
                "SCHEMA_NOT_MIGRATED",
                f"Nothing recorded; this code expects revision {expected}.",
            )
        if len(found) > 1:
            # Two recorded revisions are not "the right one plus something
            # harmless". Nobody can say what this database has been migrated by,
            # and which of them a single read returned is a property of the
            # engine rather than of the deployment.
            return _blocked(
                "DATABASE_SCHEMA",
                "SCHEMA_MULTIPLE_REVISIONS",
                f"{len(found)} revisions recorded; this code expects only {expected}.",
            )
        if not is_current(found, expected):
            # Behind, ahead and unrelated are all "not the schema this code was
            # written against", and none of them is safe to run over.
            return _blocked(
                "DATABASE_SCHEMA",
                "SCHEMA_REVISION_MISMATCH",
                f"Database at revision {next(iter(found))}; this code expects {expected}.",
            )
        return _satisfied("DATABASE_SCHEMA", f"Database and code agree on revision {expected}.")

    async def _read_revisions(self) -> frozenset[str]:
        async with self._sessions() as session:
            connection = await session.connection()
            return await recorded_revisions(connection)

    async def _pause(self, errors: list[str]) -> Check:
        """The durable stop, read the way every other reader reads it.

        Fails closed: a control that cannot be read is not a control that says
        no stop is in force. The account row is read, never taken and never
        written.
        """
        reader = AccountPauseReader(self._sessions)
        try:
            paused = await self._bounded(reader.system_paused())
        except SystemPauseUnavailable:
            return _blocked(
                "ACCOUNT_PAUSE",
                "PAUSE_STATE_UNAVAILABLE",
                "The paper account is missing, so no stop can be read from it.",
            )
        except TimeoutError:
            errors.append("DATABASE_TIMEOUT")
            return _check(
                "ACCOUNT_PAUSE",
                CheckStatus.UNAVAILABLE,
                reason="DATABASE_TIMEOUT",
                note="The database did not answer within the configured step bound.",
            )
        except (SQLAlchemyError, OSError):
            errors.append("DATABASE_UNAVAILABLE")
            return _check(
                "ACCOUNT_PAUSE",
                CheckStatus.UNAVAILABLE,
                reason="DATABASE_UNAVAILABLE",
                note="The durable stop could not be read.",
            )
        if paused:
            return _blocked(
                "ACCOUNT_PAUSE",
                "ACCOUNT_PAUSED",
                "A durable stop is in force; a run would refuse to act.",
            )
        return _satisfied("ACCOUNT_PAUSE", "No durable stop is recorded.")

    async def _bounded[T](self, work: Coroutine[Any, Any, T]) -> T:
        """Every local read, held to the bound a run puts on one step.

        A check that can hang is not a check. `wait_for` cancels the read and
        awaits that cancellation, so nothing continues against the database
        after this returns.
        """
        return await asyncio.wait_for(work, timeout=max(0.001, self._timeout))

    # --------------------------------------------------------- what it cannot know

    def _unverifiable(self) -> list[Check]:
        """Everything that would take an outside call, named rather than assumed.

        This is the half of a readiness report that is usually missing. A
        configured key is a string in an environment; whether the other end
        accepts it is a question only the other end can answer, and answering it
        here would mean spending somebody's credit to produce a fact that is
        stale the moment it is printed.
        """
        settings = self._settings
        found: list[Check] = [
            _satisfied(
                "CREDENTIALS_PRESENT",
                "Every selected provider has a credential configured; settings refuse otherwise.",
            ),
            _external(
                "CREDENTIAL_VALIDITY",
                "Whether a configured credential is accepted is only knowable by using it.",
            ),
            _external(
                "MARKET_DATA_CURRENT",
                "A market is current only at the instant a run asks.",
                reason="TRANSIENT_AT_RUN_TIME",
            ),
        ]
        if settings.market_provider != "fixture" or settings.vector_history_provider != "disabled":
            found.append(
                _external("MARKET_PROVIDER_REACHABLE", "No request is made to the market provider.")
            )
        if settings.reasoning_provider not in ("disabled", "fake"):
            found.append(
                _external(
                    "MODEL_PROVIDER_REACHABLE", "No model call is made, and none is paid for."
                )
            )
        if settings.evm_runtime_enabled:
            found.append(_external("CHAIN_RPC_REACHABLE", "No RPC endpoint is contacted."))
        if settings.execution_quote_provider != "disabled":
            found.append(_external("QUOTE_PROVIDER_REACHABLE", "No quote is requested."))
        if self._fact_sources(settings):
            found.append(_external("FACT_SOURCE_REACHABLE", "No indexer or social source is read."))
        return found

    @staticmethod
    def _fact_sources(settings: Settings) -> bool:
        """Whether any indexer or social source is selected at all.

        Asked of the factories that decide it, which put exactly the configured
        providers into their routing tables and leave the rest out. One selected
        holder source is one this check cannot reach, and a condition of this
        shape once required *all four* before it noticed — so a deployment with
        a single indexer was reported as having nothing external to verify.

        Constructing a source starts nothing and calls nobody, which is the
        factories' own stated contract.
        """
        from src.agents.atlas.sources.factory import holder_sources, origin_sources
        from src.agents.signal.sources.factory import social_source

        return bool(
            holder_sources(settings).sources
            or origin_sources(settings).sources
            or social_source(settings) is not None
        )


async def preflight(
    settings: Settings, *, ports: RunnerPorts | None = None, clock: Clock | None = None
) -> PreflightReading:
    """Compose the production stack, ask it everything, and release it again.

    The engine is owned here, as it is for a run, so a check that raises still
    gives its connections back. Nothing survives this call.
    """
    engine, sessions = connect(settings.database_url)
    try:
        async with runner_stack(settings, sessions, ports=ports) as stack:
            return await Preflight(settings, sessions, clock=clock).run(stack)
    finally:
        await engine.dispose()


def refused(reason: str, note: str = "") -> PreflightReading:
    """A reading for a configuration that could not be built at all.

    A refusal: somebody has to change a setting. Blocked, no error, and the
    exit code the report itself derives says so.
    """
    return PreflightReading(
        checked_at=datetime.now(UTC).isoformat(),
        ready=False,
        checks=(_blocked("SETTINGS", reason, note),),
    )


def unavailable(reason: str, note: str = "") -> PreflightReading:
    """A reading for a check that could not be carried out at all.

    Deliberately not a refusal. Nothing was established about the
    configuration, so reporting it as blocked would tell an operator to go and
    change a setting over an event that says nothing about any setting — and
    would derive an exit code that contradicts the one the process returns.
    The error is what carries it to the technical exit.
    """
    return PreflightReading(
        checked_at=datetime.now(UTC).isoformat(),
        ready=False,
        checks=(_check("PREFLIGHT", CheckStatus.UNAVAILABLE, reason=reason, note=note),),
        errors=(reason,),
    )
