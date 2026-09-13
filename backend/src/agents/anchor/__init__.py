"""ANCHOR execution-liquidity specialist.

Answers one question for the exact setup that just triggered: what can the
current executable market support? It answers from quotes, deterministically.
There is no model, no prompt and no reasoning provider anywhere in this package.

It cannot decide whether a trade should happen, size a position, allocate
capital, judge exposure or loss limits, approve anything, reach a wallet, signer
or executor, or move a TradeCase into an executable state. It produces
LIQUIDITY_EXECUTION_EVIDENCE and nothing else.

What it reports is market capacity, not permission — and where a bounded ladder
passed every size it tried, it says so rather than presenting the top of that
ladder as a limit.
"""

from src.agents.anchor.assessment import assess, execution_deviation_bps
from src.agents.anchor.context import AnchorContextReader, market_context, reference_market
from src.agents.anchor.handler import (
    ANCHOR_TASK_TYPE,
    AnchorWorkerHandler,
    execution_digest,
)
from src.agents.anchor.models import (
    ANCHOR_OUTPUT_SCHEMA_VERSION,
    AnchorMarketContext,
    AnchorReasonCode,
    AnchorTaskInput,
    CapacitySemantics,
    ExecutionAssessment,
    QuoteAttempt,
    QuotedPoint,
    ReferenceMarket,
    RejectionReason,
)
from src.agents.anchor.policy import ANCHOR_EXECUTION_V1, AnchorExecutionPolicy
from src.agents.anchor.ports import AnchorContextPort, AnchorContextUnavailable

__all__ = [
    "ANCHOR_EXECUTION_V1",
    "ANCHOR_OUTPUT_SCHEMA_VERSION",
    "ANCHOR_TASK_TYPE",
    "AnchorContextPort",
    "AnchorContextReader",
    "AnchorContextUnavailable",
    "AnchorExecutionPolicy",
    "AnchorMarketContext",
    "AnchorReasonCode",
    "AnchorTaskInput",
    "AnchorWorkerHandler",
    "CapacitySemantics",
    "ExecutionAssessment",
    "QuoteAttempt",
    "QuotedPoint",
    "ReferenceMarket",
    "RejectionReason",
    "assess",
    "execution_deviation_bps",
    "execution_digest",
    "market_context",
    "reference_market",
]
