"""Deterministic PAPER entry sizing: what was asked for, never what is allowed.

Nothing here constructs a `TradeIntent`, asks SENTINEL, touches a wallet or
reaches an executor. The output is one typed reading of a configured amount
against a recorded price, and a reading permits nothing.
"""

from src.orchestration.sizing.calculator import assess_paper_sizing
from src.orchestration.sizing.context import (
    PaperSizingReader,
    SizingCaseSource,
    SizingMarketInput,
)
from src.orchestration.sizing.models import (
    BaseAssetMetadata,
    ReferencePrice,
    SizingAssessment,
    SizingOutcome,
    SizingReading,
    SizingRefusal,
    SizingRefused,
    sizing_input_digest,
)
from src.orchestration.sizing.policy import PAPER_SIZING_V1, PaperSizingPolicy

__all__ = [
    "PAPER_SIZING_V1",
    "BaseAssetMetadata",
    "PaperSizingPolicy",
    "PaperSizingReader",
    "ReferencePrice",
    "SizingAssessment",
    "SizingCaseSource",
    "SizingMarketInput",
    "SizingOutcome",
    "SizingReading",
    "SizingRefusal",
    "SizingRefused",
    "assess_paper_sizing",
    "sizing_input_digest",
]
