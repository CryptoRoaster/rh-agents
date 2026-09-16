"""What one bounded PAPER run is, what it may report, and how it ends.

A run is an *attempt to make progress*, not a promise of a trade. It opens what
intake allows, lets the specialists that are actually configured take one step
each where a step is available, asks SENTINEL about whatever became ready, and
fills what SENTINEL approved. Every one of those may legitimately produce
nothing, and a run that produces nothing and says why is a successful run.

Three things this is not.

**Not a loop.** One pass. When no role can claim another task, the run ends
rather than waiting for one to appear. Work that is not due yet stays in the
task table for the next explicit run — the loop lives there, durably, and not
inside a process somebody has to keep alive.

**Not an authority.** It calls the same public service contracts a test calls.
Status belongs to the workflow, task handling to the runtime, sizing to the
sizing contract and verdicts to SENTINEL. The run coordinates; it decides
nothing.

**Not an identity.** `run_id` exists so one pass can be found in logs. No order,
case, request or fill is ever derived from it — those come from business
identities that survive a restart, which a run id by construction does not.
"""

from datetime import timedelta
from enum import IntEnum, StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Code = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^\S(?:.*\S)?$")]


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class ExitCode(IntEnum):
    """What the process returns, and nothing more than what it means.

    Deliberately three values. A business refusal is not a failure of the run —
    the run did exactly what it exists to do and reported a stop — so it is not
    worth an exit code, and conflating it with one would make an operator treat
    "SENTINEL said no" as an outage. What the caller actually needs to branch on
    is: did this run get to do its work, was it refused before it started, or
    did something break.
    """

    # The run completed. Cases may be waiting, refused, or filled; the summary
    # says which, and a run that reached a budget still completed.
    COMPLETED = 0
    # Something broke: the database was unreachable, a service raised. Nothing
    # here says anything about a market.
    TECHNICAL_FAILURE = 1
    # The configuration does not permit a run, detected before any mutating
    # step. Nothing was attempted and nothing was written.
    CONFIGURATION_REFUSED = 2


class RunStop(StrEnum):
    """Why the pass ended. Exactly one of these is true of any run."""

    # Nothing left that this run may do: no claimable task, nothing ready, and
    # nothing approved. The ordinary ending.
    NOTHING_LEFT_TO_DO = "NOTHING_LEFT_TO_DO"
    STEP_BUDGET_REACHED = "STEP_BUDGET_REACHED"
    TIME_BUDGET_REACHED = "TIME_BUDGET_REACHED"
    CASE_BUDGET_REACHED = "CASE_BUDGET_REACHED"
    # A stop was in force. The run did not attempt to work around it.
    SYSTEM_STOPPED = "SYSTEM_STOPPED"


class RunLimits(Immutable):
    """The bounds one run is held to. All upper limits, none a target."""

    max_candidates: int = Field(ge=1, le=50)
    max_steps: int = Field(ge=1, le=500)
    max_cases: int = Field(ge=1, le=20)
    max_runtime_seconds: int = Field(ge=5, le=3600)
    step_timeout_seconds: int = Field(ge=1, le=300)

    @property
    def runtime(self) -> timedelta:
        return timedelta(seconds=self.max_runtime_seconds)

    @property
    def step_timeout(self) -> timedelta:
        return timedelta(seconds=self.step_timeout_seconds)


class RoleAvailability(Immutable):
    """Whether a specialist could run at all, and why not when it could not.

    Reported rather than silently skipped. A run in which four of seven roles
    were never wired looks identical to one in which they had nothing to do,
    and those are very different situations for whoever is reading the output.
    """

    role: Identifier
    available: bool = Field(strict=True)
    reason: Code | None = None


class CaseProgress(Immutable):
    """What happened to one case in this run."""

    trade_case_id: UUID
    status: Identifier
    # The workflow's own reason code for where it stands. Safe by construction:
    # these are the codes the workflow already publishes.
    reason_code: Code | None = None
    risk_outcome: Code | None = None
    risk_refusal: Code | None = None
    fill_refusal: Code | None = None
    execution_id: UUID | None = None
    replayed: bool = Field(default=False, strict=True)


class RunSummary(Immutable):
    """One structured account of one pass, safe to print anywhere.

    Nothing here carries a secret, a provider payload or an exception text. What
    it carries is counts, identifiers this system already exposes, and reason
    codes drawn from the typed vocabularies the services publish.
    """

    kind: Literal["paper_run_summary"] = "paper_run_summary"
    run_id: UUID
    started_at: str
    finished_at: str
    stop: RunStop
    limits: RunLimits
    roles: tuple[RoleAvailability, ...] = Field(default=(), max_length=16)
    candidates_seen: int = Field(default=0, ge=0)
    cases_opened: int = Field(default=0, ge=0)
    intake_refusals: tuple[Code, ...] = Field(default=(), max_length=64)
    steps_taken: int = Field(default=0, ge=0)
    cases: tuple[CaseProgress, ...] = Field(default=(), max_length=64)
    risk_requests: int = Field(default=0, ge=0)
    fills: int = Field(default=0, ge=0)
    replays: int = Field(default=0, ge=0)
    # Technical faults, as codes. Never a provider message and never a traceback.
    errors: tuple[Code, ...] = Field(default=(), max_length=32)

    @property
    def exit_code(self) -> ExitCode:
        return ExitCode.TECHNICAL_FAILURE if self.errors else ExitCode.COMPLETED

    @property
    def waiting(self) -> tuple[CaseProgress, ...]:
        """Cases this run left standing: neither filled nor finally refused."""
        return tuple(
            item for item in self.cases if item.execution_id is None and item.risk_outcome is None
        )


class ConfigurationRefused(Immutable):
    """The run was not permitted to start. Nothing was attempted."""

    kind: Literal["run_configuration_refused"] = "run_configuration_refused"
    reason: Code
    detail: Code | None = None

    @property
    def exit_code(self) -> ExitCode:
        return ExitCode.CONFIGURATION_REFUSED


class TechnicalFailure(Immutable):
    """The run could not be carried out. Says nothing about any market.

    Kept apart from `ConfigurationRefused` on purpose: one means the operator
    has not permitted a run, the other that something broke while performing
    one, and an operator reading the output has to be able to tell which.
    """

    kind: Literal["run_technical_failure"] = "run_technical_failure"
    reason: Code

    @property
    def exit_code(self) -> ExitCode:
        return ExitCode.TECHNICAL_FAILURE


RunReading = RunSummary | ConfigurationRefused | TechnicalFailure
