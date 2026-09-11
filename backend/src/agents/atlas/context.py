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
    HolderFactsSourceResult,
    Immutable,
    OriginFacts,
    OriginVerification,
)
from src.agents.atlas.ports import (
    ContractOriginReadPort,
    CreationVerificationPort,
    HolderIntelligenceReadPort,
    TokenContractReadPort,
)
from src.agents.atlas.sources.normalize import HolderNormalizationError, concentration
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
    # Optional chain-side confirmation of what a creation provider claims. Absent
    # means claims stay UNVERIFIED, never silently trusted.
    verifier: CreationVerificationPort | None = None
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
        # Contract facts come first because the holder denominator is the
        # on-chain total supply, never a figure the holder provider supplies.
        holders = await self._holder_facts(chain, token_address, contract)
        origin = await self._origin_facts(chain, token_address)
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

    async def _holder_facts(
        self, chain: ChainSnapshot, token_address: str, contract: ContractFacts
    ) -> HolderFacts:
        try:
            result = await self.holders.holder_facts(chain.chain, token_address)
        except Exception:
            return HolderFacts(
                status=Availability.UNAVAILABLE,
                failure=AtlasSourceFailure.UNAVAILABLE,
                source="unknown",
            )
        if result.status != Availability.AVAILABLE:
            return HolderFacts(status=result.status, failure=result.failure, source=result.source)
        return self._holder_measurement(result, chain, token_address, contract)

    def _holder_measurement(
        self,
        result: HolderFactsSourceResult,
        chain: ChainSnapshot,
        token_address: str,
        contract: ContractFacts,
    ) -> HolderFacts:
        """Turn provider rows into the measured fact, or into an honest failure.

        Every concentration is computed here from raw balances and the on-chain
        supply. A provider's own percentage is never used, so a vendor cannot
        move a safety metric by disagreeing with arithmetic.
        """

        def unusable(failure: AtlasSourceFailure) -> HolderFacts:
            return HolderFacts(
                status=Availability.UNAVAILABLE, failure=failure, source=result.source
            )

        if result.token_address != token_address:
            # The provider answered about a different token.
            return unusable(AtlasSourceFailure.TOKEN_MISMATCH)
        if result.chain != chain.chain:
            return unusable(AtlasSourceFailure.CHAIN_MISMATCH)
        supply = contract.total_supply_raw if contract.status == Availability.AVAILABLE else None
        if supply is not None and result.provider_total_supply_raw == 0 and supply > 0:
            # The two views of the same contract are not merely skewed, they are
            # incompatible. Reconciliation is exact: no tolerance is guessed.
            return unusable(AtlasSourceFailure.SUPPLY_INCONSISTENT)
        try:
            measured = concentration(result.rows, supply, result.completeness)
        except HolderNormalizationError as error:
            return unusable(error.failure)
        return HolderFacts(
            status=Availability.AVAILABLE,
            source=result.source,
            # Anchored to what the source observed, never to when we fetched it.
            observed_at=result.snapshot_timestamp,
            observation_basis=result.observation_basis,
            completeness=result.completeness,
            snapshot_block=result.snapshot_block,
            holder_block_delta=(
                None
                if result.snapshot_block is None
                else chain.block_number - result.snapshot_block
            ),
            holder_count=result.holder_count,
            total_supply_raw=supply,
            top_holders=measured.shares,
            top1_share=measured.top1_share,
            top5_share=measured.top5_share,
            top10_share=measured.top10_share,
            top10_share_excluding_burn=measured.top10_share_excluding_burn,
            burned_raw=measured.burned_raw,
            burned_share=measured.burned_share,
        )

    async def _origin_facts(self, chain: ChainSnapshot, token_address: str) -> OriginFacts:
        try:
            facts = await self.origins.origin_facts(chain.chain, token_address)
        except Exception:
            return OriginFacts(
                status=Availability.UNAVAILABLE,
                failure=AtlasSourceFailure.UNAVAILABLE,
                source="unknown",
            )
        if facts.status != Availability.AVAILABLE or self.verifier is None:
            return facts
        return await self._verified_origin(self.verifier, facts, chain, token_address)

    @staticmethod
    async def _verified_origin(
        verifier: CreationVerificationPort,
        facts: OriginFacts,
        chain: ChainSnapshot,
        token_address: str,
    ) -> OriginFacts:
        """Check a creation claim against the chain rather than against JSON."""
        created: str | None = None
        creator_is_contract: bool | None = None
        try:
            if facts.creation_tx_hash is not None:
                created = await verifier.creation_receipt_contract(facts.creation_tx_hash)
            if facts.creator_address is not None:
                creator_is_contract = await verifier.is_contract(
                    facts.creator_address, chain.block_number
                )
        except Exception:
            # A failed check leaves the claim unverified; it never confirms it.
            created = None
        if created is not None and created != token_address:
            # The named transaction created some other contract, so the creator
            # it names is not this token's creator.
            return OriginFacts(
                status=Availability.UNAVAILABLE,
                failure=AtlasSourceFailure.INVALID_RESPONSE,
                source=facts.source,
            )
        return facts.model_copy(
            update={
                "creator_is_contract": creator_is_contract,
                "verification": (
                    OriginVerification.RECEIPT_CONFIRMED
                    if created is not None
                    else OriginVerification.UNVERIFIED
                ),
            }
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
        "block_timestamp": snapshot.chain.block_timestamp.isoformat(),
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
            "observation_basis": (
                None if holders.observation_basis is None else holders.observation_basis.value
            ),
            "completeness": holders.completeness.value,
            "snapshot_block": holders.snapshot_block,
            "holder_block_delta": holders.holder_block_delta,
            "total_supply_raw": (
                None if holders.total_supply_raw is None else str(holders.total_supply_raw)
            ),
            "burned_raw": None if holders.burned_raw is None else str(holders.burned_raw),
            "burned_share": (
                None if holders.burned_share is None else canonical_decimal(holders.burned_share)
            ),
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
                    # The exact balance, so two positions that round to the same
                    # share still produce different digests.
                    "balance_raw": str(holder.balance_raw),
                    "share": canonical_decimal(holder.share),
                    "is_burn_address": holder.is_burn_address,
                    "is_contract": holder.is_contract,
                }
                for holder in holders.top_holders[:20]
            ],
        },
        "origin": {
            **_measurement(snapshot.origin.status, snapshot.origin.failure),
            "source": snapshot.origin.source,
            "creator_address": snapshot.origin.creator_address,
            "creation_block": snapshot.origin.creation_block,
            "creation_tx_hash": snapshot.origin.creation_tx_hash,
            "factory_address": snapshot.origin.factory_address,
            "creator_is_contract": snapshot.origin.creator_is_contract,
            "verification": snapshot.origin.verification.value,
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
