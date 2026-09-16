"""Controlled model answers for the specialists, derived from the real prompt.

Not a canned payload. Each reply is built from the document the handler actually
put in front of the model, so it names the observation, pair and chain the
context really contained — which is what the validators check, and what a fixed
fixture would get wrong the moment anything upstream changed.

This replaces exactly one external boundary: the model. Every context reader,
validator, workflow rule and evidence contract downstream of it is the real one.
"""

from dataclasses import dataclass, field
from typing import Any

from src.reasoning.models import (
    ReasoningErrorCategory,
    ReasoningFailure,
    ReasoningModel,
    ReasoningResult,
    ReasoningUsage,
)


@dataclass
class ScriptedSpecialists:
    """One provider for every role, answering from the request it is given."""

    calls: list[str] = field(default_factory=list)
    provider_name: str = "scripted-specialists"

    @property
    def name(self) -> str:
        return self.provider_name

    async def generate_structured(self, request: Any) -> Any:
        model = request.output_model
        self.calls.append(model.__name__)
        builder = BUILDERS.get(model.__name__)
        if builder is None:
            # Nothing scripted for this role. Reported as a provider failure
            # rather than raised, because that is the answer a real provider
            # could give and every handler already has a contract for it —
            # ATLAS, for instance, treats commentary as advisory and proceeds.
            raise ReasoningFailure(
                ReasoningErrorCategory.PROVIDER_NOT_CONFIGURED, "NO_SCRIPTED_ANSWER"
            )
        return ReasoningResult(
            output=model.model_validate(builder(request.data)),
            model=ReasoningModel(provider=self.provider_name, model="scripted"),
            usage=ReasoningUsage(input_tokens=1, output_tokens=1),
        )


def _orbit(data: dict[str, Any]) -> dict[str, Any]:
    """An interesting market, citing the observation the context supplied."""
    observation = data["market_observation"]
    return {
        "classification": "INTERESTING",
        "strength": "MODERATE",
        "reason_codes": ["PRICE_AVAILABLE"],
        "data_gaps": [],
        "cited_observation_ids": [observation["snapshot_id"]],
        "pair_id": observation["pair_id"],
        "chain": observation["chain"],
        "summary": "Price and liquidity are present on the supplied observation.",
    }


def _signal(data: dict[str, Any]) -> dict[str, Any]:
    """A reading of tone that claims nothing the sample cannot support.

    Neutral, weak, and no stated demand at all — the one answer that is valid
    whatever breadth the deterministic half measured, and the same shape the
    risk-data suites already treat as sufficient sentiment evidence.
    """
    return {
        "sentiment_direction": "NEUTRAL",
        "sentiment_strength": "WEAK",
        "social_demand_indication": "NONE",
        "narrative_tags": [],
        "manipulation_observations": [],
        "cited_observation_ids": [],
        "summary": "Discussion is present and leans neither way.",
    }


def _vector(data: dict[str, Any]) -> dict[str, Any]:
    """A breakout above the observed price, wrong well below it.

    The geometry is the one the VECTOR suite already proves coherent against a
    series centred on the same price, scaled to whatever price the context
    actually reports — so this stays a valid proposal if the recorded market
    changes rather than a fixture that silently stops being grounded.
    """
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    market = data["market_context"]
    spot = Decimal(str(market["price"]["value_usd"]))
    evaluated = datetime.fromisoformat(str(market["evaluated_at"]))
    if evaluated.tzinfo is None:  # pragma: no cover - the context is always aware
        evaluated = evaluated.replace(tzinfo=UTC)
    entry = (spot * Decimal("1.10")).quantize(Decimal("0.00000001"))
    return {
        "kind": "BREAKOUT_LONG",
        "side": "BUY",
        "entry_low": str(entry),
        "entry_high": str(entry),
        "invalidation_price": str((spot * Decimal("0.92")).quantize(Decimal("0.00000001"))),
        "targets": [
            str((spot * Decimal("1.15")).quantize(Decimal("0.00000001"))),
            str((spot * Decimal("1.20")).quantize(Decimal("0.00000001"))),
        ],
        "expires_at": (evaluated + timedelta(hours=2)).isoformat(),
        "reason_codes": ["PRICE_AVAILABLE", "LIQUIDITY_PRESENT"],
        "cited_observation_ids": [market["snapshot_id"]],
        "summary": "A move through the level would confirm; below the floor it is wrong.",
    }


BUILDERS: dict[str, Any] = {
    "OrbitAssessment": _orbit,
    "SignalAssessment": _signal,
    "VectorSetupProposal": _vector,
}
