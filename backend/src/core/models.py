"""Shared contracts. Monetary amounts use Decimal and USD as the paper quote currency."""

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Amount = Annotated[Decimal, Field(ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18)]
Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False, max_digits=38, decimal_places=18)]
SignedAmount = Annotated[Decimal, Field(allow_inf_nan=False, max_digits=38, decimal_places=18)]
Fraction = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
Identifier = Annotated[str, Field(min_length=1, max_length=200)]


def utc_now() -> datetime:
    return datetime.now(UTC)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class Record(Contract):
    id: UUID = Field(default_factory=uuid4)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    source: Identifier
    correlation_id: UUID


class TradingMode(StrEnum):
    OBSERVE = "OBSERVE"
    PAPER = "PAPER"
    LIVE_AUTONOMOUS = "LIVE_AUTONOMOUS"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class SafetyStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class RiskOutcome(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    PAUSE_SYSTEM = "PAUSE_SYSTEM"


class AgentRole(StrEnum):
    ORBIT = "ORBIT"
    ATLAS = "ATLAS"
    SIGNAL = "SIGNAL"
    VECTOR = "VECTOR"
    PULSE = "PULSE"
    ANCHOR = "ANCHOR"
    FUSE = "FUSE"
    COMMANDER = "COMMANDER"


class ExecutionTiming(Contract):
    detected_at: AwareDatetime
    decision_at: AwareDatetime | None = None
    risk_approved_at: AwareDatetime | None = None
    execution_requested_at: AwareDatetime | None = None
    tx_signed_at: AwareDatetime | None = None
    tx_sent_at: AwareDatetime | None = None
    tx_confirmed_at: AwareDatetime | None = None


class TokenSnapshot(Record):
    asset_id: Identifier  # Chain-qualified identifier, e.g. paper:DEMO.
    symbol: Identifier
    decimals: int = Field(ge=0, le=36)
    tradable: SafetyStatus = SafetyStatus.UNKNOWN


class LiquiditySnapshot(Record):
    asset_id: Identifier
    liquidity_usd: Amount | None = None
    routing: SafetyStatus = SafetyStatus.UNKNOWN
    estimated_slippage_bps: Amount | None = None


class HolderSnapshot(Record):
    asset_id: Identifier
    holder_count: int | None = Field(default=None, ge=0)
    top_ten_fraction: Fraction | None = None
    concentration_check: SafetyStatus = SafetyStatus.UNKNOWN


class WalletActivity(Record):
    asset_id: Identifier
    wallet_address: Identifier  # Public identifier only.
    side: Side
    quantity: Positive
    observed_at: AwareDatetime


class MarketSnapshot(Record):
    asset_id: Identifier
    observed_at: AwareDatetime
    price_usd: Positive
    liquidity: LiquiditySnapshot
    token: TokenSnapshot
    holders: HolderSnapshot
    fee_bps: Amount | None = None

    def fingerprint(self) -> str:
        return sha256(self.model_dump_json().encode()).hexdigest()

    @model_validator(mode="after")
    def matching_assets(self) -> Self:
        if any(x.asset_id != self.asset_id for x in (self.liquidity, self.token, self.holders)):
            raise ValueError("Snapshot asset identifiers must agree")
        return self


class TradeSetup(Record):
    asset_id: Identifier
    entry_price: Positive
    invalidation_price: Positive
    target_prices: tuple[Positive, ...] = Field(min_length=1)
    expires_at: AwareDatetime


class TradeIntent(Record):
    asset_id: Identifier
    side: Side
    quantity: Positive
    signal_price: Positive
    max_slippage_bps: Amount = Field(le=10000)
    mode: TradingMode = TradingMode.PAPER
    timing: ExecutionTiming

    def fingerprint(self) -> str:
        return sha256(self.model_dump_json().encode()).hexdigest()


class Observation(Contract):
    kind: Literal["observation"] = "observation"
    asset_id: Identifier
    assessment: Literal["OPPORTUNITY", "WATCH", "AVOID", "UNKNOWN"]
    evidence_ids: tuple[UUID, ...] = Field(min_length=1)


class SetupProposal(Contract):
    kind: Literal["setup"] = "setup"
    setup: TradeSetup


class IntentProposal(Contract):
    kind: Literal["trade_intent"] = "trade_intent"
    intent: TradeIntent


class AgentDecision(Record):
    agent: AgentRole
    decision_at: AwareDatetime
    confidence: Fraction
    rationale: Annotated[str, Field(min_length=1, max_length=4000)]
    payload: Annotated[Observation | SetupProposal | IntentProposal, Field(discriminator="kind")]
    latency_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_proposal(self) -> Self:
        if isinstance(self.payload, IntentProposal):
            if self.agent not in (AgentRole.FUSE, AgentRole.COMMANDER):
                raise ValueError("Only FUSE and COMMANDER propose trade intents")
            if self.payload.intent.correlation_id != self.correlation_id:
                raise ValueError("Intent must preserve the decision correlation ID")
        return self


class RiskLimits(Contract):
    kill_switch: bool = Field(default=False, strict=True)
    max_exposure_usd: Positive = Decimal("10000")
    max_position_size_usd: Positive = Decimal("2500")
    max_slippage_bps: Amount = Field(default=Decimal("100"), le=10000)
    daily_loss_limit_usd: Positive = Decimal("500")
    min_liquidity_usd: Positive = Decimal("100000")
    max_top_ten_holder_fraction: Fraction = Decimal("0.35")
    max_snapshot_age_seconds: int = Field(default=30, gt=0)
    approval_ttl_seconds: int = Field(default=5, gt=0)


class RiskContext(Contract):
    cash_usd: Amount | None = None
    exposure_usd: Amount | None = None
    position_quantity: Amount | None = None
    daily_loss_usd: Amount | None = None
    accounting: SafetyStatus = SafetyStatus.UNKNOWN


class RiskMetrics(Contract):
    requested_notional_usd: Amount
    worst_case_notional_usd: Amount
    exposure_usd: Amount | None
    daily_loss_usd: Amount | None
    liquidity_usd: Amount | None
    estimated_slippage_bps: Amount | None


class RiskDecision(Record):
    intent_id: UUID
    intent_fingerprint: str
    market_snapshot_id: UUID
    market_fingerprint: str
    outcome: RiskOutcome
    reason_codes: tuple[str, ...] = Field(min_length=1)
    position_size_limit_usd: Amount = Field(
        description="Absolute configured per-position USD limit"
    )
    max_additional_notional_usd: Amount = Field(
        description="Additional BUY quote notional before costs; zero for SELL or unsafe context. "
        "Sizing guidance only, never an execution authorization."
    )
    max_slippage_bps: Amount
    metrics: RiskMetrics
    evaluated_at: AwareDatetime
    expires_at: AwareDatetime

    @model_validator(mode="before")
    @classmethod
    def read_legacy_sizing(cls, value: object) -> object:
        # Historical records lacked incremental capacity. Preserve replay without
        # inventing it or rewriting immutable events; all new records use both fields.
        if (
            isinstance(value, dict)
            and "max_allowed_position_size_usd" in value
            and "position_size_limit_usd" not in value
            and "max_additional_notional_usd" not in value
        ):
            value = dict(value)
            value["position_size_limit_usd"] = value.pop("max_allowed_position_size_usd")
            value["max_additional_notional_usd"] = Decimal("0")
        return value


class OrderIntent(Record):
    intent: TradeIntent
    risk: RiskDecision
    execution_requested_at: AwareDatetime

    @model_validator(mode="after")
    def approved_and_bound(self) -> Self:
        if self.risk.outcome != RiskOutcome.APPROVE:
            raise ValueError("Order requires deterministic risk approval")
        if self.risk.intent_id != self.intent.id:
            raise ValueError("Approval belongs to another intent")
        if self.risk.intent_fingerprint != self.intent.fingerprint():
            raise ValueError("Intent changed after risk evaluation")
        if self.intent.mode != TradingMode.PAPER:
            raise ValueError("Only PAPER orders are supported in Phase 0")
        if not (self.correlation_id == self.intent.correlation_id == self.risk.correlation_id):
            raise ValueError("Order, risk, and intent must share a trace")
        if not self.risk.evaluated_at <= self.execution_requested_at <= self.risk.expires_at:
            raise ValueError("Risk approval is expired or not yet valid")
        return self


class ExecutionResult(Record):
    order_id: UUID
    intent_id: UUID
    asset_id: Identifier
    side: Side
    mode: Literal[TradingMode.PAPER] = TradingMode.PAPER
    status: Literal["FILLED"] = "FILLED"
    quantity: Positive
    signal_price: Positive
    quote_price: Positive
    execution_price: Positive
    estimated_slippage_bps: Amount
    realized_slippage_bps: Amount
    fees_usd: Amount
    gas_usd: Amount = Decimal("0")
    execution_latency_ms: int = Field(ge=0)
    timing: ExecutionTiming


class Position(Record):
    asset_id: Identifier
    # The market this position was acquired in, when it is known. An asset is
    # not a market — a token can trade in several pools — so valuing a position
    # needs the pair it came from rather than a market chosen for its asset.
    # Absent on anything acquired before this was recorded, and absence is
    # reported rather than resolved.
    market_pair_id: Identifier | None = None
    market_chain: Identifier | None = None
    market_network: Identifier | None = None
    market_provider: Identifier | None = None
    quantity: Amount = Decimal("0")
    cost_basis_usd: Amount = Decimal("0")
    realized_pnl_usd: SignedAmount = Decimal("0")


class Trade(Record):
    execution_id: UUID
    position_id: UUID
    asset_id: Identifier
    side: Side
    quantity: Positive
    price_usd: Positive
    fees_usd: Amount
    realized_pnl_usd: SignedAmount


class PnLSnapshot(Record):
    cash_usd: Amount
    market_value_usd: Amount
    equity_usd: Amount
    realized_pnl_usd: SignedAmount
    unrealized_pnl_usd: SignedAmount
    total_pnl_usd: SignedAmount
    fees_paid_usd: Amount
