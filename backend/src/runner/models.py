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
    # A market observation may or may not have been recorded, and this run
    # cannot say which. Everything after it would be a trading decision taken
    # over data of unknown provenance, so the pass ends instead.
    ACQUISITION_OUTCOME_UNKNOWN = "ACQUISITION_OUTCOME_UNKNOWN"
    STEP_BUDGET_REACHED = "STEP_BUDGET_REACHED"
    TIME_BUDGET_REACHED = "TIME_BUDGET_REACHED"
    CASE_BUDGET_REACHED = "CASE_BUDGET_REACHED"
    # A stop was in force. The run did not attempt to work around it.
    SYSTEM_STOPPED = "SYSTEM_STOPPED"


class RunLimits(Immutable):
    """The bounds one run is held to. All upper limits, none a target."""

    # How many recorded candidates one pass may *process*. Intake reads and
    # judges up to this many; most of them are refused for reasons that have
    # nothing to do with a budget.
    max_candidates: int = Field(ge=1, le=50)
    # How many of them may become new cases. A different question from the one
    # above and answered by a different number: looking at a market costs a
    # read, taking it on creates work somebody has to finish or expire.
    max_new_cases: int = Field(ge=1, le=50)
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


class SourceRefresh(Immutable):
    """One request for a source to be observed again, and what came of it.

    Reported per source rather than as a single verdict for the case, because a
    case can need two different observers and the answer for one says nothing
    about the other.
    """

    # A `RiskFactOrigin` value: which source was asked about.
    origin: Code
    # A `SourceRefreshOutcome` value: what the workflow answered.
    outcome: Code
    role: Code | None = None
    # The attempt the observing task was moved to. Present only when one really
    # was armed, so a refusal cannot be read as progress.
    attempt: int | None = Field(default=None, ge=1)


class AcquisitionNeed(StrEnum):
    """Why one market was on this run's acquisition list.

    Reported per market because the four are not interchangeable. A market the
    portfolio must be valued against is a precondition for *every* fill; one a
    case needs is a precondition for that case alone; and a new candidate is not
    a precondition for anything — it is work this run may take on if the budget
    it did not spend on the first three allows.
    """

    # An open position's own market. Without it the portfolio cannot be marked,
    # and SENTINEL refuses every case rather than judging a partial portfolio.
    POSITION_VALUATION = "POSITION_VALUATION"
    # The market an existing, non-terminal case is about.
    CASE_MARKET = "CASE_MARKET"
    # The market that prices a case's payment asset in dollars. A different
    # reading from the pair's own, and one ANCHOR refuses to infer.
    QUOTE_ASSET = "QUOTE_ASSET"
    # Anything a bounded discovery read returned. Never a recommendation: what
    # it produces is a recorded observation, which intake may or may not open a
    # case from under its own rules.
    NEW_CANDIDATE = "NEW_CANDIDATE"


class AcquisitionOutcome(StrEnum):
    """What came of asking about one market. Six answers, deliberately.

    The distinctions that matter are between *what was durably written by this
    run*, *what was already there*, *what was declined and will stay declined
    until something changes*, *what broke*, and *what nobody can currently say*.
    Collapsing any two of those would let a run report progress it did not make.
    """

    # This run's own write, confirmed durable by the recorder.
    RECORDED = "RECORDED"
    # Nothing new was written, and nothing needed to be: the event was already
    # stored, or the same market had already been observed earlier in this very
    # pass. A replay, and never counted as an observation this run made.
    UNCHANGED = "UNCHANGED"
    # A typed refusal before or during the request. No observation, and no
    # reason to think a retry would change the answer.
    REFUSED = "REFUSED"
    # The provider or the database failed. Says nothing about the market.
    FAILED = "FAILED"
    # The call was cut off after it may have committed. This run does not know
    # whether the observation exists, and says so rather than guessing.
    UNKNOWN = "UNKNOWN"
    # Planned, and never asked about, because a budget ran out first. Reported
    # so a missing market is visibly a budget decision rather than a silence.
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


class AcquisitionStop(StrEnum):
    """Why the acquisition stage ended."""

    COMPLETED = "COMPLETED"
    NOT_ENABLED = "NOT_ENABLED"
    # The stage was switched on and this configuration cannot perform it. A
    # mistake somebody made, and not a statement about any market.
    CONFIGURATION_REFUSED = "CONFIGURATION_REFUSED"
    # Nothing needed acquiring and no discovery was permitted.
    NOTHING_TO_ACQUIRE = "NOTHING_TO_ACQUIRE"
    MARKET_BUDGET_REACHED = "MARKET_BUDGET_REACHED"
    REQUEST_BUDGET_REACHED = "REQUEST_BUDGET_REACHED"
    TIME_BUDGET_REACHED = "TIME_BUDGET_REACHED"
    # A stop was in force. No provider was called.
    SYSTEM_STOPPED = "SYSTEM_STOPPED"
    # The stop could not be read, or no stop source is configured at all.
    # Different from being stopped, and treated the same way: unknown is not
    # permission to spend somebody's provider budget.
    SYSTEM_STOP_UNREADABLE = "SYSTEM_STOP_UNREADABLE"
    PROVIDER_FAILED = "PROVIDER_FAILED"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class AcquisitionLimits(Immutable):
    """The bounds one acquisition stage is held to.

    Kept apart from `RunLimits` on purpose, and not merely for tidiness: a
    candidate budget bounds how much recorded market intake may *judge*, a step
    budget bounds how much work the specialists may do, and these bound what a
    run may *ask a public provider for*. They bound different costs, paid to
    different parties, and one number covering all of them would mean tightening
    the provider spend by quietly doing less analysis.
    """

    # Distinct markets this run may have observed again. Duplicated needs cost
    # nothing: one market is one observation however many things wanted it.
    max_markets: int = Field(ge=1, le=20)
    # Bounded discovery reads across all configured chains. Zero is a real and
    # useful setting: acquire exactly what the open work depends on, and look
    # for nothing new.
    max_discovery_requests: int = Field(ge=0, le=4)
    # Logical provider requests and HTTP attempts, the second including every
    # retry and every helper query such as network resolution. Both are applied
    # to the provider's own budgets rather than beside them.
    max_provider_requests: int = Field(ge=1, le=10)
    max_http_attempts: int = Field(ge=1, le=10)
    max_seconds: int = Field(ge=1, le=600)

    @property
    def runtime(self) -> timedelta:
        return timedelta(seconds=self.max_seconds)


class AcquiredMarket(Immutable):
    """One market on the list, and what became of it."""

    pair_id: Identifier
    chain: Identifier
    # An `AcquisitionNeed` value.
    need: Code
    # An `AcquisitionOutcome` value.
    outcome: Code
    # A provider error code, a recorder refusal or a planning refusal. Always
    # one of this system's own codes, never a provider message.
    reason: Code | None = None


class MarketAcquisition(Immutable):
    """What one run asked the market provider for, and what it got.

    Counted as it happens rather than assembled at the end, for the same reason
    the run's own account is: an observation that was durably recorded stays
    recorded whatever fails afterwards, and a summary built after a failure
    would report zero and be wrong about the world.
    """

    kind: Literal["market_acquisition"] = "market_acquisition"
    enabled: bool = Field(strict=True)
    # An `AcquisitionStop` value.
    stop: Code
    limits: AcquisitionLimits | None = None
    # Markets this run asked the provider about by identity, plus the markets a
    # discovery read returned. Never the number of entries below: two needs
    # pointing at one market are one request.
    requested: int = Field(default=0, ge=0)
    recorded: int = Field(default=0, ge=0)
    unchanged: int = Field(default=0, ge=0)
    refused: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    unknown: int = Field(default=0, ge=0)
    not_attempted: int = Field(default=0, ge=0)
    # What the provider transport actually spent, read from its own counters.
    # The second includes retries, so the two differ exactly when something was
    # retried — which is the fact an operator needs and a single number hides.
    provider_requests: int = Field(default=0, ge=0)
    http_attempts: int = Field(default=0, ge=0)
    markets: tuple[AcquiredMarket, ...] = Field(default=(), max_length=64)

    @property
    def outcome_unknown(self) -> bool:
        """Whether anything this stage did may or may not have happened."""
        return self.unknown > 0 or self.stop == AcquisitionStop.OUTCOME_UNKNOWN.value


class CaseProgress(Immutable):
    """What happened to one case in this run."""

    trade_case_id: UUID
    status: Identifier
    # The workflow's own reason code for where it stands. Safe by construction:
    # these are the codes the workflow already publishes.
    reason_code: Code | None = None
    risk_outcome: Code | None = None
    risk_refusal: Code | None = None
    # What came of asking for sources to be observed again, when this run asked.
    # Empty means it never needed to: nothing had aged out, or what stopped the
    # case was not something a new observation could fix.
    refreshes: tuple[SourceRefresh, ...] = Field(default=(), max_length=8)
    fill_refusal: Code | None = None
    execution_id: UUID | None = None
    # A decisive call was cut off before it answered. It may have committed and
    # it may not, and this run does not know which — so it says so rather than
    # inventing either. The next explicit run addresses the same order key and
    # finds out what really happened.
    outcome_unknown: bool = Field(default=False, strict=True)
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
    # Considered by intake, and the subset that became cases. Reported apart
    # because they answer different questions about one pass.
    candidates_seen: int = Field(default=0, ge=0)
    cases_opened: int = Field(default=0, ge=0)
    # The intake cycle did not return an answer. It commits one case at a time,
    # so an interrupted cycle may have opened some and this run cannot say how
    # many — the count stays zero and this says why, rather than a number nobody
    # confirmed or a claim that nothing happened.
    intake_outcome_unknown: bool = Field(default=False, strict=True)
    intake_refusals: tuple[Code, ...] = Field(default=(), max_length=64)
    steps_taken: int = Field(default=0, ge=0)
    # Attempts that began and were cut off. Counted apart from an empty claim,
    # because "there was no work" and "the work did not finish" are different
    # facts about a run and lead to different questions.
    steps_timed_out: int = Field(default=0, ge=0)
    # What this run asked the market provider for before it traded anything.
    # Absent when acquisition is switched off, which is the ordinary case and
    # is what every run before this contract did.
    acquisition: MarketAcquisition | None = None
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
