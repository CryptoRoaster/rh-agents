"""EARLY contracts: the facts a pre-VECTOR setup is built from, and nothing more.

EARLY is not an analyst. It holds no model and no opinion, and the only thing
it can produce is the fixed PRE_VECTOR_EARLY_ENTRY_V1 geometry around a fresh
recorded price — and only for a market VECTOR's own sufficiency check called
too young, whose chain-side age ATLAS recorded and which is no older than the
strategy allows.
"""

from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from src.markets.models import MarketPrice

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")]
Code = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class EarlyTaskInput(Immutable):
    """Everything the producer is given. No session, client, wallet or executor."""

    trade_case_id: UUID
    task_id: UUID
    strategy_policy_id: Identifier
    workflow_version: Identifier
    pair_id: Identifier
    chain: Identifier
    network: Identifier
    base_asset_id: Identifier
    # The fresh recorded price the geometry is drawn around.
    market_snapshot_id: UUID
    price_observation_id: UUID
    reference_price: MarketPrice
    price_observed_at: AwareDatetime
    # VECTOR's own verdicts on VECTOR's own request.
    vector_sufficiency: Code
    young_history_sufficiency: Code | None = None
    closed_bars: int = Field(ge=0)
    history_provider: Identifier
    history_timeframe: Identifier
    history_aggregate: int = Field(gt=0)
    history_requested_bars: int = Field(gt=0)
    history_policy_version: Identifier
    # ATLAS's chain-side record of when the token was created.
    creation_block: int = Field(ge=0)
    creation_timestamp: AwareDatetime
    creation_time_source: Identifier
    onchain_evidence_id: UUID
    candidate_age_seconds: int = Field(ge=0)
    max_age_seconds: int = Field(gt=0)
    evaluated_at: AwareDatetime
    supersedes_evidence_id: UUID | None = None
