"""Optional bounded live smoke test. Never runs in CI and never runs by default.

Opt in explicitly with RH_AGENTS_LIVE_REASONING_SMOKE=1 and a configured
ANTHROPIC_API_KEY. This makes one small paid request, so it is deliberately not
enabled by the presence of a key alone: an ambient credential is not consent to
spend on it. Nothing here trades, signs, broadcasts or writes financial state.
"""

import os
from uuid import uuid4

import pytest
from pydantic import SecretStr

from src.agents.orbit.context import reasoning_payload
from src.agents.orbit.models import OrbitAssessment
from src.agents.orbit.prompt import ORBIT_INSTRUCTIONS
from src.agents.orbit.validation import validate_assessment
from src.reasoning.anthropic_provider import AnthropicReasoningProvider
from src.reasoning.models import ReasoningRequest
from tests.orbit.conftest import reader_for

LIVE = os.environ.get("RH_AGENTS_LIVE_REASONING_SMOKE") == "1"
KEY = os.environ.get("ANTHROPIC_API_KEY", "")

pytestmark = pytest.mark.skipif(
    not (LIVE and KEY), reason="Live reasoning smoke test is opt-in and needs a configured key"
)


async def test_live_provider_returns_a_schema_valid_assessment(snapshot, now, trace):
    reader = reader_for(snapshot, now, trace)
    task_input = await reader.candidate_context(reader.cases.trade_case.id, uuid4())
    provider = AnthropicReasoningProvider(
        api_key=SecretStr(KEY),
        model=os.environ.get("REASONING_MODEL", "claude-opus-5"),
    )
    result = await provider.generate_structured(
        ReasoningRequest(
            instructions=ORBIT_INSTRUCTIONS,
            data=reasoning_payload(task_input),
            output_model=OrbitAssessment,
            max_output_tokens=1024,
            timeout_seconds=60.0,
        )
    )
    # Provider-side schema enforcement never replaces local validation.
    validate_assessment(result.output, task_input)
    assert result.model.provider == "anthropic"
    # Only safe metadata is ever reported; no key, header or raw response.
    print(
        f"provider={result.model.provider} model={result.model.model} "
        f"latency_ms={result.usage.latency_ms} classification={result.output.classification.value}"
    )
