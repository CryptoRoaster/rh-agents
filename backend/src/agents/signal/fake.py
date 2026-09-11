"""Deterministic scripted social source.

Exists so the whole SIGNAL pipeline can be exercised offline and reproducibly,
against the shapes real campaigns actually take. It performs no I/O, is never a
default, and production use would be meaningless: it returns exactly what a
caller scripted.

No real provider is integrated in this phase. That is a deliberate choice rather
than an omission — the alternative was scraping a platform that does not offer an
API on terms we can meet, and a safety-critical input sourced by circumventing an
access control is worse than no input at all.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from src.agents.signal.models import (
    MarketBindingBasis,
    ObservationEngagement,
    ObservationKind,
    SignalObservation,
    SignalSource,
    SignalWindow,
)
from src.agents.signal.ports import SignalSourceUnavailable

FAKE_PROVIDER = "fake-social"


def observation_id(label: str) -> UUID:
    """A stable identifier for a fixture post, so digests stay comparable."""
    return uuid5(NAMESPACE_URL, f"rh-agents:signal:{label}")


def observation(
    label: str,
    *,
    created_at: datetime,
    author: str,
    text: str,
    source: SignalSource = SignalSource.X,
    kind: ObservationKind = ObservationKind.ORIGINAL,
    basis: MarketBindingBasis = MarketBindingBasis.CONTRACT_ADDRESS_EXACT,
    address: str | None = None,
    chain: str | None = None,
    referenced: UUID | None = None,
    likes: int | None = None,
    reposts: int | None = None,
) -> SignalObservation:
    """One fixture observation. ``received_at`` is deliberately later than creation.

    Every fixture is collected after it was written, which is the normal case and
    the one that would hide a fetch-time freshness bug if the two were equal.
    """
    return SignalObservation(
        observation_id=observation_id(label),
        source=source,
        source_native_id=label,
        author_id=author,
        kind=kind,
        created_at=created_at,
        received_at=created_at + timedelta(minutes=5),
        content=text,
        language="en",
        engagement=(
            None
            if likes is None and reposts is None
            else ObservationEngagement(likes=likes, reposts=reposts)
        ),
        referenced_observation_id=referenced,
        binding_basis=basis,
        binding_address=address,
        binding_chain=chain,
        provider=FAKE_PROVIDER,
    )


@dataclass
class DeterministicSignalSource:
    """Replays one scripted observation set, or a scripted source failure.

    The set is returned unfiltered on purpose. Window and binding rules belong to
    the collector, so a fixture can hand back stale or wrongly bound posts and the
    test proves they are rejected downstream rather than never offered.
    """

    scripted: tuple[SignalObservation, ...] = ()
    failure: str | None = None
    calls: list[tuple[str, str, SignalWindow]] = field(default_factory=list)

    @classmethod
    def unavailable(cls, reason_code: str = "SOURCE_UNAVAILABLE") -> "DeterministicSignalSource":
        return cls(failure=reason_code)

    async def observations(
        self, *, chain: str, pair_id: str, window: SignalWindow
    ) -> tuple[SignalObservation, ...]:
        self.calls.append((chain, pair_id, window))
        if self.failure is not None:
            raise SignalSourceUnavailable(self.failure)
        return self.scripted
