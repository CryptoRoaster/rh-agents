"""One ORBIT evaluation: request, model call, validation, digest.

The part of ORBIT that does not care who is asking. The TradeCase worker wraps
the result in an evidence envelope; the early-discovery scout stores it as a
watch assessment. Both get it from here, so there is one prompt, one validator
and one digest — never a second ORBIT whose disagreements with the first nobody
could see.

Failures are not translated. A `ReasoningFailure` and an `OrbitValidationError`
reach the caller unchanged, because what a failure *means* is the caller's
contract: a task retry category for the worker, a recorded failed assessment for
the scout.
"""

from dataclasses import dataclass
from datetime import timedelta

from src.agents.orbit.context import orbit_input_digest, reasoning_payload
from src.agents.orbit.models import OrbitAssessment, OrbitEvaluationInput
from src.agents.orbit.prompt import ORBIT_INSTRUCTIONS
from src.agents.orbit.validation import validate_assessment
from src.reasoning.models import ReasoningModel, ReasoningRequest, ReasoningResult, ReasoningUsage
from src.reasoning.provider import ReasoningProvider


@dataclass(frozen=True)
class OrbitEvaluation:
    """A validated assessment and the facts needed to audit it."""

    assessment: OrbitAssessment
    input_digest: str
    model: ReasoningModel
    usage: ReasoningUsage


@dataclass(frozen=True)
class OrbitEvaluator:
    provider: ReasoningProvider
    max_output_tokens: int = 1024
    timeout: timedelta = timedelta(seconds=60)

    async def evaluate(self, evaluation_input: OrbitEvaluationInput) -> OrbitEvaluation:
        """Ask the model once and accept only an answer consistent with its input.

        Raises `ReasoningFailure` when the provider fails and
        `OrbitValidationError` when the answer contradicts what it was shown.
        Contradicted output is never returned, not even as an unknown.
        """
        digest = orbit_input_digest(evaluation_input)
        request: ReasoningRequest[OrbitAssessment] = ReasoningRequest(
            instructions=ORBIT_INSTRUCTIONS,
            data=reasoning_payload(evaluation_input),
            output_model=OrbitAssessment,
            max_output_tokens=self.max_output_tokens,
            timeout_seconds=self.timeout.total_seconds(),
        )
        result: ReasoningResult[OrbitAssessment] = await self.provider.generate_structured(request)
        validate_assessment(result.output, evaluation_input)
        return OrbitEvaluation(
            assessment=result.output,
            input_digest=digest,
            model=result.model,
            usage=result.usage,
        )
