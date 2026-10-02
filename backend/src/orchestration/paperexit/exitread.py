"""A fresh, exit-specific on-chain read for an open PAPER position.

The entry's ATLAS evidence belongs to a terminal case: it was valid for the
purchase, it cannot be refreshed, and it must not be reopened. A sale is judged
on its own read instead, taken when the sale is asked for.

The read is ATLAS's deterministic half and nothing more: the same approved
ports, the same snapshot builder, the same versioned policy and the same
payload derivation the worker submits. No model is asked, nothing is written to
the case, and the result exists only as the basis of this one exit, stored
beside it.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from src.agents.atlas.context import (
    AtlasContextUnavailable,
    SnapshotBuilderPort,
    atlas_snapshot_digest,
)
from src.agents.atlas.handler import onchain_payload
from src.agents.atlas.policy import ATLAS_EXIT_POLICY_V2, AtlasPolicy, evaluate_snapshot
from src.core.clock import Clock, SystemClock
from src.markets.models import MarketIdentity
from src.orchestration.workflow.models import OnchainPayload


class ExitReadUnavailable(Exception):
    """No fresh on-chain read could be taken. Safe reason code only."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class ExitOnchainRead:
    """What the chain said about the held token, for this exit alone."""

    trade_case_id: UUID
    payload: OnchainPayload
    policy_version: str
    snapshot_digest: str
    observed_at: datetime
    collected_at: datetime

    def basis(self) -> dict[str, object]:
        return {
            "source": "ATLAS_DETERMINISTIC_EXIT_READ",
            "policy_version": self.policy_version,
            "snapshot_digest": self.snapshot_digest,
            "observed_at": self.observed_at.isoformat(),
            "collected_at": self.collected_at.isoformat(),
            "payload": self.payload.model_dump(mode="json"),
        }


class ExitOnchainReadPort(Protocol):
    async def read(
        self, trade_case_id: UUID, market: MarketIdentity, request_key: str
    ) -> ExitOnchainRead: ...


@dataclass(frozen=True)
class AtlasExitRead:
    """ATLAS's collector and policy, run for a sale instead of for a case."""

    builder: SnapshotBuilderPort
    policy: AtlasPolicy = ATLAS_EXIT_POLICY_V2
    clock: Clock = SystemClock()

    async def read(
        self, trade_case_id: UUID, market: MarketIdentity, request_key: str
    ) -> ExitOnchainRead:
        # Derived from the exit's key, so a retry of the same exit names the
        # same read rather than inventing a task.
        task_id = uuid5(NAMESPACE_URL, f"rh-agents:paper-exit-read:{request_key}")
        try:
            snapshot = await self.builder.build(trade_case_id, task_id, market)
        except AtlasContextUnavailable as error:
            raise ExitReadUnavailable(error.reason_code) from None
        decision = evaluate_snapshot(snapshot, self.clock.now(), self.policy)
        return ExitOnchainRead(
            trade_case_id=trade_case_id,
            payload=onchain_payload(snapshot, decision),
            policy_version=decision.policy_version,
            snapshot_digest=atlas_snapshot_digest(snapshot),
            observed_at=snapshot.chain.observed_at,
            collected_at=snapshot.collected_at,
        )


@dataclass(frozen=True)
class UnavailableExitRead:
    """No chain source is configured: every exit read is unavailable, by name."""

    reason_code: str

    async def read(
        self, trade_case_id: UUID, market: MarketIdentity, request_key: str
    ) -> ExitOnchainRead:
        raise ExitReadUnavailable(self.reason_code)


__all__ = [
    "AtlasExitRead",
    "ExitOnchainRead",
    "ExitOnchainReadPort",
    "ExitReadUnavailable",
    "UnavailableExitRead",
]
