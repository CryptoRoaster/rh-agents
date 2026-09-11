"""Provider-neutral contracts for one structured reasoning call.

This is a narrow structured-reasoning port, not an agent framework. A provider
receives typed input and returns typed output. It is never handed a database
session, RPC client, HTTP client, filesystem, shell or tool surface, and it
cannot reach one through this contract.
"""

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S(?:.*\S)?$")]
Code = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]


class Immutable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class ReasoningErrorCategory(StrEnum):
    """Why a reasoning call failed, in terms the worker runtime can act on."""

    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_RATE_LIMIT = "PROVIDER_RATE_LIMIT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_REFUSED = "PROVIDER_REFUSED"
    PROVIDER_REJECTED_REQUEST = "PROVIDER_REJECTED_REQUEST"
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    INVALID_MODEL_OUTPUT = "INVALID_MODEL_OUTPUT"


class ReasoningFailure(Exception):
    """Typed provider refusal. Never carries a key, header or raw vendor payload."""

    def __init__(self, category: ReasoningErrorCategory, reason_code: str) -> None:
        self.category = category
        self.reason_code = reason_code
        super().__init__(f"{category.value}:{reason_code}")


class ReasoningUsage(Immutable):
    """Safe call metadata. Never authoritative for any workflow decision."""

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    provider_request_id: Identifier | None = None


class ReasoningModel(Immutable):
    """Which reasoning produced an output, for provenance and later upgrades."""

    provider: Identifier
    model: Identifier
    effort: Identifier | None = None


class ReasoningRequest[Output: BaseModel](Immutable):
    """One structured call: fixed instructions plus quoted data, nothing else.

    ``instructions`` is the only control channel. ``data`` is untrusted input that
    the provider must treat as quoted content, never as instructions, which is why
    the two are separate fields rather than one concatenated string.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    instructions: str = Field(min_length=1, max_length=20000)
    data: dict[str, object]
    output_model: type[Output]
    max_output_tokens: int = Field(ge=64, le=8192)
    timeout_seconds: float = Field(gt=0, le=300)

    @model_validator(mode="after")
    def separated_channels(self) -> Self:
        if not self.data:
            raise ValueError("A reasoning request must carry structured data")
        return self


class ReasoningResult[Output: BaseModel](Immutable):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    output: Output
    model: ReasoningModel
    usage: ReasoningUsage
