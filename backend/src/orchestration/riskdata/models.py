"""What a risk evaluation needs, whether it is there, and where it came from.

This is a completeness contract, not a risk contract. It answers one question —
*are the market and safety facts SENTINEL requires present, current, correctly
attributed and correctly understood?* — and answers it without forming any view
about whether a trade should happen.

Three distinctions carry the design.

**Incomplete is not rejected.** A missing fact stops the check and says which
fact is missing. It never becomes a SENTINEL verdict, because manufacturing a
rejection out of a data gap would make an architectural hole look like a
judgement about a market — and in this system a risk rejection is terminal and
permanently bars the market from opening another case.

**A gap is not a blocker.** "We could not measure this" and "we measured this
and it is dangerous" are different states with different remedies, and the
second must never be relabelled as the first. Known negative evidence is
reported alongside the gaps, in both outcomes, and never quietly folded in.

**Complete is not permitted.** A complete reading says the checked data is
there. It says nothing about the requested size, the portfolio, the configured
limits or the system stops, all of which a real risk input also needs and none
of which are checked here.
"""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from src.orchestration.workflow.models import TradeCaseStatus

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")]
Code = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class RiskFactKind(StrEnum):
    """Every market or safety input the existing SENTINEL contract consumes.

    Derived from `src.risk.engine.evaluate` rather than from a summary of it:
    each member is a field that function actually reads and can refuse on. The
    portfolio side — cash, exposure, position, daily loss, accounting — is
    deliberately absent, because it comes from the paper account rather than
    from market data or specialist evidence, and is not this reader's subject.
    """

    # `MarketSnapshot.price_usd`, in USD per whole base token.
    REFERENCE_PRICE = "REFERENCE_PRICE"
    # `TokenSnapshot.symbol` and `.decimals`.
    TOKEN_METADATA = "TOKEN_METADATA"
    # `TokenSnapshot.tradable`.
    TOKEN_TRADABILITY = "TOKEN_TRADABILITY"
    # `LiquiditySnapshot.liquidity_usd`, checked against `min_liquidity_usd`.
    LIQUIDITY_DEPTH = "LIQUIDITY_DEPTH"
    # `LiquiditySnapshot.routing`.
    ROUTING_AVAILABILITY = "ROUTING_AVAILABILITY"
    # `MarketSnapshot.fee_bps`.
    EXECUTION_FEE_BASIS = "EXECUTION_FEE_BASIS"
    # `LiquiditySnapshot.estimated_slippage_bps`.
    EXECUTION_SLIPPAGE_BASIS = "EXECUTION_SLIPPAGE_BASIS"
    # `HolderSnapshot.holder_count`.
    HOLDER_COUNT = "HOLDER_COUNT"
    # `HolderSnapshot.top_ten_fraction`, checked against a configured limit.
    HOLDER_CONCENTRATION = "HOLDER_CONCENTRATION"
    # `HolderSnapshot.concentration_check`.
    HOLDER_INTEGRITY = "HOLDER_INTEGRITY"


class RiskFactOrigin(StrEnum):
    """Where a fact is allowed to come from. Nothing else may supply one."""

    RECORDED_MARKET_OBSERVATION = "RECORDED_MARKET_OBSERVATION"
    ATLAS_ONCHAIN_EVIDENCE = "ATLAS_ONCHAIN_EVIDENCE"
    ANCHOR_EXECUTION_EVIDENCE = "ANCHOR_EXECUTION_EVIDENCE"
    # An operator's stated simulation assumption. Never an observation, and
    # marked as such so it can never be read back as one.
    OPERATOR_CONFIGURED_ASSUMPTION = "OPERATOR_CONFIGURED_ASSUMPTION"


class RiskDataGapCode(StrEnum):
    """Why a required fact is not usable. Each names a cause, not a field."""

    NOT_RECORDED = "NOT_RECORDED"
    NOT_ESTABLISHED = "NOT_ESTABLISHED"
    STALE = "STALE"
    OBSERVED_IN_THE_FUTURE = "OBSERVED_IN_THE_FUTURE"
    WRONG_ASSET = "WRONG_ASSET"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    # The holder source could not prove how much of the distribution it saw, so
    # no concentration may be derived from it.
    HOLDER_COVERAGE_UNPROVEN = "HOLDER_COVERAGE_UNPROVEN"
    # The provider filtered addresses out of the holder list before we saw it.
    # The numerator is then a lower bound while the denominator stays full
    # supply, so the metric can only understate concentration — and an
    # understated figure passing a limit is the failure a limit exists to
    # prevent. Usable as context, never as a threshold input.
    HOLDER_METRIC_UNDERSTATED = "HOLDER_METRIC_UNDERSTATED"
    # The evidence carries a verdict but no measured figure. A `PASS` says the
    # domain met its prerequisites; it is not a number and cannot stand in for one.
    VERDICT_WITHOUT_METRIC = "VERDICT_WITHOUT_METRIC"


class RiskDataOutcome(StrEnum):
    """One value, so it cannot be read as anything broader than it is."""

    RISK_DATA_COMPLETE = "RISK_DATA_COMPLETE"


class RiskFact(Immutable):
    """One usable input, with the provenance that makes it checkable."""

    kind: RiskFactKind
    origin: RiskFactOrigin
    # Unit and semantics in one code, so a later builder never has to infer
    # either from a field name. `USD_PER_BASE_UNIT` and `FRACTION_OF_TOTAL_SUPPLY`
    # are different kinds of number even where both are decimals in [0, 1].
    meaning: Code
    # The asset this fact is about, canonically. `None` only where the fact is
    # about no particular asset, which today is the configured cost basis.
    asset_id: Identifier | None = None
    source: Identifier
    # When the fact was true according to its own source, never when it was read.
    observed_at: AwareDatetime | None = None
    # When it stops being usable, where that is knowable. A configured
    # assumption has no expiry, and says so by carrying none.
    valid_until: AwareDatetime | None = None


class RiskDataGap(Immutable):
    """One required input that is not usable, and why."""

    kind: RiskFactKind
    code: RiskDataGapCode
    # The origin that would have supplied it, so a reader knows where to look.
    expected_origin: RiskFactOrigin


class RiskDataBlocker(Immutable):
    """Established negative evidence, carried beside the gaps and never inside them."""

    code: Code
    origin: RiskFactOrigin | None = None
    evidence_id: UUID | None = None


class RiskDataReadiness(Immutable):
    """The state of the checked inputs for one case.

    Both outcomes carry the same structure on purpose: the facts that *are*
    present are worth reporting even when something is missing, and the blockers
    are worth reporting even when nothing is.
    """

    kind: Literal["risk_data_readiness"] = "risk_data_readiness"
    outcome: RiskDataOutcome | None = None
    policy_version: Identifier
    trade_case_id: UUID
    base_asset_id: Identifier | None = None
    trade_case_status: TradeCaseStatus
    facts: tuple[RiskFact, ...] = Field(default=(), max_length=24)
    gaps: tuple[RiskDataGap, ...] = Field(default=(), max_length=24)
    blockers: tuple[RiskDataBlocker, ...] = Field(default=(), max_length=32)
    observed_at: AwareDatetime
    # The earliest future expiry among the facts this reading rests on, so a
    # caller can refuse to act on a reading that has aged out rather than
    # repeating a conclusion frozen at read time.
    valid_until: AwareDatetime | None = None

    @property
    def complete(self) -> bool:
        return self.outcome is RiskDataOutcome.RISK_DATA_COMPLETE

    def is_current_at(self, instant: datetime) -> bool:
        """Half-open, like every other validity in this system."""
        return self.valid_until is None or instant < self.valid_until

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.complete and self.gaps:
            raise ValueError("A complete reading cannot carry a data gap")
        if not self.complete and not self.gaps:
            raise ValueError("An incomplete reading must name what is missing")
        seen = [item.kind for item in self.facts]
        if len(set(seen)) != len(seen):
            raise ValueError("One fact per kind")
        covered = set(seen) | {item.kind for item in self.gaps}
        if covered != set(RiskFactKind):
            raise ValueError("Every required input must be reported as a fact or as a gap")
        return self
