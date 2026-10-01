"""Primitive ATLAS types shared by the fact models and the V4 pool-control facts.

Kept apart from `models` only so that both can import them without a cycle;
`models` re-exports every name, which remains the place to import them from.
"""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

EvmAddress = Annotated[str, Field(strict=True, pattern=r"^0x[0-9a-f]{40}$")]
Hash32 = Annotated[str, Field(strict=True, pattern=r"^0x[0-9a-f]{64}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")]
SafeSummary = Annotated[str, Field(min_length=1, max_length=600)]
Ratio = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class AtlasSourceFailure(StrEnum):
    """Why a source could not answer, mapped safely from provider detail."""

    NOT_CONFIGURED = "NOT_CONFIGURED"
    UNSUPPORTED_CHAIN = "UNSUPPORTED_CHAIN"
    UNSUPPORTED_ENDPOINT = "UNSUPPORTED_ENDPOINT"
    REQUIRES_ARCHIVE = "REQUIRES_ARCHIVE"
    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    CHAIN_MISMATCH = "CHAIN_MISMATCH"
    TOKEN_MISMATCH = "TOKEN_MISMATCH"
    INCOMPLETE_RESULT = "INCOMPLETE_RESULT"
    SUPPLY_INCONSISTENT = "SUPPLY_INCONSISTENT"
    DENOMINATOR_UNKNOWN = "DENOMINATOR_UNKNOWN"
