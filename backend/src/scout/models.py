"""What a watch is, what a scout review recorded, and what a scout run reports."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from src.markets.models import MarketIdentity
from src.runner.models import Code, ExitCode, Identifier, Immutable
from src.scout.policy import WatchStatus


class DiscoveryWatch(Immutable):
    """One persistent watch, as stored."""

    id: UUID
    schema_version: int
    policy_version: Identifier
    provider: Identifier
    chain: Identifier
    network: Identifier
    pair_id: Identifier
    is_fixture: bool = Field(strict=True)
    market: MarketIdentity
    first_seen_at: AwareDatetime
    last_seen_at: AwareDatetime
    latest_snapshot_id: UUID
    created_at: AwareDatetime
    updated_at: AwareDatetime
    status: WatchStatus
    next_orbit_review_at: AwareDatetime | None = None
    orbit_checkpoint_index: int | None = None
    next_history_review_at: AwareDatetime | None = None
    latest_vector_sufficiency: Code | None = None
    vector_checked_at: AwareDatetime | None = None
    reason_code: Code
    last_promoted_trade_case_id: UUID | None = None

    def age_seconds(self, now: datetime) -> int:
        return max(0, int((now - self.first_seen_at).total_seconds()))


class WatchAssessment(Immutable):
    """One scout ORBIT review. Discovery history, never TradeCase evidence."""

    id: UUID
    watch_id: UUID
    snapshot_id: UUID
    assessed_at: AwareDatetime
    checkpoint_index: int
    checkpoint_seconds: int
    status: Literal["COMPLETED", "FAILED"]
    failure_reason: Code | None = None
    classification: Code | None = None
    strength: Code | None = None
    reason_codes: tuple[Code, ...] = ()
    data_gaps: tuple[Code, ...] = ()
    cited_observation_ids: tuple[UUID, ...] = ()
    summary: str | None = None
    input_digest: str
    policy_version: Identifier
    prompt_version: Identifier
    prompt_hash: str
    output_schema_version: int
    reasoning_provider: Identifier | None = None
    reasoning_model: Identifier | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None


class ScoutReview(Immutable):
    """One review this run took, in codes. No summary text and no market data."""

    watch_id: UUID
    pair_id: Identifier
    watch_age_seconds: int = Field(ge=0)
    checkpoint_seconds: int = Field(ge=0)
    status: Literal["COMPLETED", "FAILED"]
    failure_reason: Code | None = None
    classification: Code | None = None
    strength: Code | None = None
    reason_codes: tuple[Code, ...] = ()
    data_gaps: tuple[Code, ...] = ()
    next_review_at: str | None = None


class ScoutSummary(Immutable):
    """One structured account of one scout run, safe to print anywhere.

    Counts, identifiers this system already publishes and typed codes. No
    provider payload, no model output text and no exception message.
    """

    kind: Literal["early_scout_summary"] = "early_scout_summary"
    # Present when the run was recorded in the run history.
    run_id: UUID | None = None
    policy_version: Identifier
    started_at: str
    finished_at: str
    stop: Code
    bootstrapped: int = Field(default=0, ge=0)
    discovered: int = Field(default=0, ge=0)
    valid_markets: int = Field(default=0, ge=0)
    # Pools the adapter refused, split so coverage is countable:
    # discovered = valid_markets + provider_identity_rejects + other_provider_rejects.
    provider_identity_rejects: int = Field(default=0, ge=0)
    other_provider_rejects: int = Field(default=0, ge=0)
    rejections: tuple[Code, ...] = Field(default=(), max_length=32)
    watches_created: int = Field(default=0, ge=0)
    watches_updated: int = Field(default=0, ge=0)
    refreshed: int = Field(default=0, ge=0)
    watches_due_orbit: int = Field(default=0, ge=0)
    orbit_reviews_started: int = Field(default=0, ge=0)
    orbit_reviews_completed: int = Field(default=0, ge=0)
    interesting: int = Field(default=0, ge=0)
    not_interesting: int = Field(default=0, ge=0)
    insufficient_data: int = Field(default=0, ge=0)
    watches_due_history: int = Field(default=0, ge=0)
    history_checks: int = Field(default=0, ge=0)
    vector_sufficient: int = Field(default=0, ge=0)
    promotable_new: int = Field(default=0, ge=0)
    dormant_new: int = Field(default=0, ge=0)
    retired_new: int = Field(default=0, ge=0)
    provider_failures: int = Field(default=0, ge=0)
    model_failures: int = Field(default=0, ge=0)
    provider_requests: int = Field(default=0, ge=0)
    # Whether ORBIT keeps up: due reviews when the review phase began and when it
    # ended, the oldest due review's age, and watches never reviewed at all.
    orbit_backlog_before: int = Field(default=0, ge=0)
    orbit_backlog_after: int = Field(default=0, ge=0)
    oldest_orbit_due_age_seconds: int | None = Field(default=None, ge=0)
    new_watches_without_orbit_assessment: int = Field(default=0, ge=0)
    reviews: tuple[ScoutReview, ...] = Field(default=(), max_length=16)
    errors: tuple[Code, ...] = Field(default=(), max_length=16)
    # Stated in every summary, because it is the whole contract of this mode.
    trade_cases_opened: Literal[0] = 0

    @property
    def exit_code(self) -> ExitCode:
        return ExitCode.TECHNICAL_FAILURE if self.errors else ExitCode.COMPLETED


class ScoutRun(Immutable):
    """One persisted scout run, as the cockpit reads it."""

    id: UUID
    started_at: AwareDatetime
    completed_at: AwareDatetime
    status: Literal["COMPLETED", "STOPPED", "FAILED"]
    stop: Code
    errors: tuple[Code, ...] = ()
    policy_version: Identifier
    discovered: int
    valid_markets: int
    provider_identity_rejects: int
    other_provider_rejects: int
    watches_created: int
    watches_updated: int
    bootstrapped: int
    refreshed: int
    watches_due_orbit: int
    orbit_reviews_started: int
    orbit_reviews_completed: int
    interesting: int
    not_interesting: int
    insufficient_data: int
    watches_due_history: int
    history_checks: int
    vector_sufficient: int
    promotable_new: int
    dormant_new: int
    retired_new: int
    provider_failures: int
    model_failures: int
    provider_requests: int
    orbit_backlog_before: int
    orbit_backlog_after: int
    oldest_orbit_due_age_seconds: int | None = None
    new_watches_without_orbit_assessment: int

    @property
    def identity_acceptance_rate(self) -> float | None:
        """valid / discovered, or None when nothing was discovered."""
        return None if self.discovered == 0 else self.valid_markets / self.discovered

    @property
    def watch_creation_rate(self) -> float | None:
        """watches created / valid markets, or None when nothing was valid."""
        return None if self.valid_markets == 0 else self.watches_created / self.valid_markets
