"""Deterministic snapshot assembly and the single narrow read path ATLAS gets.

No model participates here. The builder validates the chain, pins one block,
queries only approved ports, preserves every UNKNOWN and attaches provenance.
A worker never holds a port, a session or a client — only the assembled view.
"""

import json
import re
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from src.agents.atlas.models import (
    AtlasOnchainSnapshot,
    AtlasSourceFailure,
    ChainSnapshot,
    ContractFacts,
    HolderFacts,
    Immutable,
    OriginFacts,
)
from src.agents.atlas.ports import (
    ContractOriginReadPort,
    HolderIntelligenceReadPort,
    TokenContractReadPort,
)
from src.core.clock import Clock, SystemClock
from src.core.numbers import canonical_decimal
from src.markets.models import Availability, MarketIdentity
from src.orchestration.workflow.engine import active_evidence
from src.orchestration.workflow.models import EvidenceEnvelope, EvidenceType, TradeCase

ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


class AtlasContextUnavailable(Exception):
    """No usable on-chain context. Carries a safe reason code only."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class AtlasTaskInput(Immutable):
    """What ATLAS works from. ``snapshot`` is the only part a model ever sees."""

    snapshot: AtlasOnchainSnapshot
    supersedes_evidence_id: UUID | None = None


class AtlasContextPort(Protocol):
    """ATLAS's only read capability."""

    async def onchain_context(self, trade_case_id: UUID, task_id: UUID) -> AtlasTaskInput: ...


class TradeCaseIdentitySource(Protocol):
    async def get_trade_case(self, trade_case_id: UUID) -> TradeCase: ...

    async def evidence(self, trade_case_id: UUID) -> tuple[EvidenceEnvelope, ...]: ...


def token_address_of(market: MarketIdentity) -> str:
    """The base token's contract address, or a hard failure.

    Asset identifiers are ``chain:network:address``. Anything that is not a
    20-byte EVM address is refused rather than coerced, so a bytes32 pool id can
    never be mistaken for a wallet or token contract.
    """
    candidate = market.base_asset_id.rsplit(":", 1)[-1]
    if ADDRESS.fullmatch(candidate) is None:
        raise AtlasContextUnavailable("TOKEN_ADDRESS_NOT_EVM")
    if int(candidate[2:], 16) == 0:
        raise AtlasContextUnavailable("TOKEN_ADDRESS_ZERO")
    return candidate.lower()


@dataclass(frozen=True)
class AtlasSnapshotBuilder:
    """Collects deterministic facts from approved sources. Contains no reasoning."""

    contracts: TokenContractReadPort
    holders: HolderIntelligenceReadPort
    origins: ContractOriginReadPort
    clock: Clock = SystemClock()

    async def build(
        self, trade_case_id: UUID, task_id: UUID, market: MarketIdentity
    ) -> AtlasOnchainSnapshot:
        token_address = token_address_of(market)
        chain = await self.contracts.chain_snapshot()
        if chain.chain != market.chain or chain.network != market.network:
            # Address equality means nothing across chains, so a source pointing
            # at the wrong one is a hard stop rather than a mismatch to record.
            raise AtlasContextUnavailable("SOURCE_CHAIN_MISMATCH")
        contract = await self._contract_facts(token_address, chain)
        holders = await self._holder_facts(chain.chain, token_address)
        origin = await self._origin_facts(chain.chain, token_address)
        return AtlasOnchainSnapshot(
            trade_case_id=trade_case_id,
            task_id=task_id,
            market=market,
            token_address=token_address,
            chain=chain,
            contract=contract,
            holders=holders,
            origin=origin,
            collected_at=self.clock.now(),
        )

    async def _contract_facts(self, token_address: str, chain: ChainSnapshot) -> ContractFacts:
        try:
            return await self.contracts.contract_facts(token_address, chain.block_number)
        except Exception:
            # A source that fails is unavailable, never silently absent facts.
            return ContractFacts(
                status=Availability.UNAVAILABLE,
                failure=AtlasSourceFailure.UNAVAILABLE,
                source=chain.source,
            )

    async def _holder_facts(self, chain: str, token_address: str) -> HolderFacts:
        try:
            return await self.holders.holder_facts(chain, token_address)
        except Exception:
            return HolderFacts(
                status=Availability.UNAVAILABLE,
                failure=AtlasSourceFailure.UNAVAILABLE,
                source="unknown",
            )

    async def _origin_facts(self, chain: str, token_address: str) -> OriginFacts:
        try:
            return await self.origins.origin_facts(chain, token_address)
        except Exception:
            return OriginFacts(
                status=Availability.UNAVAILABLE,
                failure=AtlasSourceFailure.UNAVAILABLE,
                source="unknown",
            )


@dataclass(frozen=True)
class AtlasContextReader:
    cases: TradeCaseIdentitySource
    builder: AtlasSnapshotBuilder

    async def onchain_context(self, trade_case_id: UUID, task_id: UUID) -> AtlasTaskInput:
        trade_case = await self.cases.get_trade_case(trade_case_id)
        snapshot = await self.builder.build(trade_case_id, task_id, trade_case.market)
        # Only ATLAS's own evidence slot is read, so no other role's findings
        # reach the worker.
        current = active_evidence(await self.cases.evidence(trade_case_id))
        existing = current.get(EvidenceType.ONCHAIN)
        return AtlasTaskInput(
            snapshot=snapshot,
            supersedes_evidence_id=existing.evidence_id if existing is not None else None,
        )


def _measurement(status: Availability, failure: AtlasSourceFailure | None) -> dict[str, object]:
    return {"status": status.value, "failure": None if failure is None else failure.value}


def snapshot_document(snapshot: AtlasOnchainSnapshot) -> dict[str, object]:
    """The bounded fact document, built once and used for both model and digest.

    Holder lists are summarized deterministically rather than shipped whole, and
    every value keeps its availability so an unobserved fact can never read as a
    measured one.
    """
    holders = snapshot.holders
    contract = snapshot.contract
    return {
        "token_address": snapshot.token_address,
        "chain": snapshot.chain.chain,
        "network": snapshot.chain.network,
        "chain_id": snapshot.chain.chain_id,
        "block_number": snapshot.chain.block_number,
        "block_observed_at": snapshot.chain.observed_at.isoformat(),
        "contract": {
            **_measurement(contract.status, contract.failure),
            "source": contract.source,
            "observed_block": contract.observed_block,
            "code_present": contract.code_present,
            "decimals": contract.decimals,
            "total_supply_raw": (
                None if contract.total_supply_raw is None else str(contract.total_supply_raw)
            ),
            "proxy": contract.proxy.value,
            "implementation_address": contract.implementation_address,
            "admin_address": contract.admin_address,
        },
        "holders": {
            **_measurement(holders.status, holders.failure),
            "source": holders.source,
            "observed_at": None if holders.observed_at is None else holders.observed_at.isoformat(),
            "holder_count": holders.holder_count,
            "top1_share": None
            if holders.top1_share is None
            else canonical_decimal(holders.top1_share),
            "top5_share": None
            if holders.top5_share is None
            else canonical_decimal(holders.top5_share),
            "top10_share": (
                None if holders.top10_share is None else canonical_decimal(holders.top10_share)
            ),
            "top10_share_excluding_burn": (
                None
                if holders.top10_share_excluding_burn is None
                else canonical_decimal(holders.top10_share_excluding_burn)
            ),
            "top_holders": [
                {
                    "address": holder.address,
                    "share": canonical_decimal(holder.share),
                    "is_burn_address": holder.is_burn_address,
                }
                for holder in holders.top_holders[:20]
            ],
        },
        "origin": {
            **_measurement(snapshot.origin.status, snapshot.origin.failure),
            "source": snapshot.origin.source,
            "creator_address": snapshot.origin.creator_address,
            "creation_block": snapshot.origin.creation_block,
        },
    }


def atlas_snapshot_digest(snapshot: AtlasOnchainSnapshot) -> str:
    """Canonical fingerprint of the established facts.

    Covers the facts and their provenance, not the moment of collection, so an
    unchanged chain state hashes identically across passes.
    """
    canonical = json.dumps(
        snapshot_document(snapshot),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=True,
    )
    return sha256(canonical.encode()).hexdigest()


def source_skew(snapshot: AtlasOnchainSnapshot) -> timedelta | None:
    if snapshot.holders.observed_at is None:
        return None
    return abs(snapshot.chain.observed_at - snapshot.holders.observed_at)
