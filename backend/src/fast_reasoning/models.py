"""Provider-neutral contracts for one fast typed assessment.

A fast assessment asks a set of typed questions (yes/no, choice, score) about
one structured state and returns typed answers with probabilities. It is a
narrow port like the reasoning port: no tools, no database, no filesystem, and
failures are typed `ReasoningFailure`s that never carry a key or a raw body.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.reasoning.models import Identifier

QuestionId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Probability = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class NoulQuestion(Frozen):
    type: Literal["noul"] = "noul"
    instructions: str = Field(min_length=1, max_length=2000)
    criteria: dict[Literal["true", "false"], str] | None = None


class ChoiceQuestion(Frozen):
    type: Literal["choice"] = "choice"
    instructions: str = Field(min_length=1, max_length=2000)
    criteria: dict[str, str] = Field(min_length=2, max_length=255)


class ScoreQuestion(Frozen):
    type: Literal["score"] = "score"
    instructions: str = Field(min_length=1, max_length=2000)
    criteria: tuple[str, ...] = Field(min_length=2, max_length=10)


Question = NoulQuestion | ChoiceQuestion | ScoreQuestion

# Distributions come back as floats; a sum this far from 1 is not a distribution.
SUM_TOLERANCE = 1e-3


class NoulAnswer(Frozen):
    type: Literal["noul"] = "noul"
    noul: Probability


class ChoiceAnswer(Frozen):
    type: Literal["choice"] = "choice"
    choice: str = Field(min_length=1, max_length=200)
    probabilities: dict[str, Probability]
    confidence: Probability

    @model_validator(mode="after")
    def distribution(self) -> "ChoiceAnswer":
        if abs(sum(self.probabilities.values()) - 1.0) > SUM_TOLERANCE:
            raise ValueError("probabilities must sum to 1")
        if self.choice not in self.probabilities:
            raise ValueError("the choice must be one of the options")
        return self


class ScoreAnswer(Frozen):
    type: Literal["score"] = "score"
    score: float = Field(ge=0.0, le=9.0, allow_inf_nan=False)
    legend: dict[str, str]
    probabilities: dict[str, Probability]
    confidence: Probability

    @model_validator(mode="after")
    def distribution(self) -> "ScoreAnswer":
        if abs(sum(self.probabilities.values()) - 1.0) > SUM_TOLERANCE:
            raise ValueError("probabilities must sum to 1")
        if set(self.probabilities) != set(self.legend):
            raise ValueError("every level needs a probability and a description")
        return self


Answer = NoulAnswer | ChoiceAnswer | ScoreAnswer


class FastRequest(Frozen):
    """One call: a structured state and named typed questions, nothing else."""

    state: dict[str, object]
    questions: dict[QuestionId, Question] = Field(min_length=1, max_length=32)


class FastResult(Frozen):
    """Answers keyed like the questions, with provenance and safe metadata."""

    answers: dict[QuestionId, Answer]
    provider: Identifier
    requested_model: Identifier
    # The versioned model the provider says answered, when it says one.
    model_version: Identifier | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int = Field(ge=0)
