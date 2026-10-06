"""Deterministic snapshot assembly and the single narrow read path ATLAS gets.

No model participates here. The builder validates the chain, pins one block,
queries only approved ports, preserves every UNKNOWN and attaches provenance.
A worker never holds a port, a session or a client — only the assembled view.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from src.agents.atlas.funding.graph import funding_graph
from src.agents.atlas.funding.models import FundingGraphFacts, FundingSourceResult
from src.agents.atlas.models import (
    RECONCILABLE_EXCLUSIONS,
    AtlasOnchainSnapshot,
    AtlasSourceFailure,
    ChainSnapshot,
    ContractFacts,
    ExclusionReconciliation,
    HolderFacts,
    HolderFactsSourceResult,
    HolderObservationBasis,
    HolderSourceRow,
    Immutable,
    OriginFacts,
    OriginVerification,
)
from src.agents.atlas.ports import (
    ContractOriginReadPort,
    CreationVerificationPort,
    FundingReadPort,
    HolderIntelligenceReadPort,
    PoolControlReadPort,
    TokenContractReadPort,
)
from src.agents.atlas.sources.normalize import HolderNormalizationError, concentration
from src.agents.atlas.v4.census import PoolControlChainRefused
from src.agents.atlas.v4.control import PositionControlFacts
from src.agents.atlas.v4.economic import pool_control
from src.agents.atlas.v4.models import PoolControlFacts, PoolControlGap, V4Census
from src.core.clock import Clock, SystemClock
from src.core.numbers import canonical_decimal
from src.markets.models import Availability, MarketIdentity, PoolLocatorKind
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


class SnapshotBuilderPort(Protocol):
    """What assembles one on-chain snapshot for a market. Routing or building."""

    async def build(
        self, trade_case_id: UUID, task_id: UUID, market: MarketIdentity
    ) -> AtlasOnchainSnapshot: ...


@dataclass(frozen=True)
class AtlasSnapshotBuilder:
    """Collects deterministic facts from approved sources. Contains no reasoning."""

    contracts: TokenContractReadPort
    holders: HolderIntelligenceReadPort
    origins: ContractOriginReadPort
    # Optional chain-side confirmation of what a creation provider claims. Absent
    # means claims stay UNVERIFIED, never silently trusted.
    verifier: CreationVerificationPort | None = None
    # The V4 pool census. Absent means none is configured: a V4 market then
    # records its pool control as unavailable, and nothing else changes.
    pool_census: PoolControlReadPort | None = None
    # The creator funding source. Absent means none is configured, and the
    # snapshot carries no funding graph at all.
    funding: FundingReadPort | None = None
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
        control = await self._pool_control(market, chain, token_address, contract, holders, origin)
        funding = await self._funding_graph(market, chain, contract, holders, origin, control)
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
            pool_control=control,
            funding_graph=funding,
        )

    async def _funding_graph(
        self,
        market: MarketIdentity,
        chain: ChainSnapshot,
        contract: ContractFacts,
        holders: HolderFacts,
        origin: OriginFacts,
        control: PoolControlFacts | None,
    ) -> FundingGraphFacts | None:
        """The creator funding measurement, from the facts already established.

        Runs last: its root is the origin creator, its window ends at the pinned
        block, and its holder overlap uses the holder basis that holder and pool
        control already settled. A shadow fact -- no policy reads it.
        """
        if self.funding is None:
            return None
        root = origin.creator_address if origin.status == Availability.AVAILABLE else None
        created = origin.creation_block
        read: FundingSourceResult | None = None
        if root is not None and created is not None and created <= chain.block_number:
            try:
                read = await self.funding.funding_transactions(
                    chain.chain, root, created, chain.block_number
                )
            except Exception:
                read = FundingSourceResult(
                    status=Availability.UNAVAILABLE,
                    failure=AtlasSourceFailure.UNAVAILABLE,
                    source=self.funding.source,
                )
        v4_market = (
            market.pool_locator is not None
            and market.pool_locator.kind == PoolLocatorKind.BYTES32_POOL_ID
        )
        return funding_graph(
            read,
            source=self.funding.source,
            origin=origin,
            contract=contract,
            holders=holders,
            pool_control=control,
            v4_required=v4_market or (control is not None and control.required),
            snapshot_block=chain.block_number,
        )

    async def _pool_control(
        self,
        market: MarketIdentity,
        chain: ChainSnapshot,
        token_address: str,
        contract: ContractFacts,
        holders: HolderFacts,
        origin: OriginFacts,
    ) -> PoolControlFacts | None:
        """Pool control for this token, or None where it was never in question.

        The census starts at the origin source's creation block, which the
        census itself proves against contract code before relying on it. Any
        failure other than a wrong chain is an unavailable census, so a V4
        token without one is unestablished rather than judged on raw holders.
        """
        v4_market = (
            market.pool_locator is not None
            and market.pool_locator.kind == PoolLocatorKind.BYTES32_POOL_ID
        )
        if self.pool_census is None:
            if not v4_market:
                return None
            census = V4Census(
                status=Availability.UNAVAILABLE,
                gap=PoolControlGap.DEPLOYMENT_NOT_CONFIGURED,
                failure=AtlasSourceFailure.NOT_CONFIGURED,
                source="unconfigured",
            )
        else:
            created = origin.creation_block if origin.status == Availability.AVAILABLE else None
            try:
                census = await self.pool_census.census(chain, token_address, created)
            except PoolControlChainRefused:
                raise AtlasContextUnavailable("SOURCE_CHAIN_MISMATCH") from None
            except Exception:
                census = V4Census(
                    status=Availability.UNAVAILABLE,
                    gap=PoolControlGap.CENSUS_UNAVAILABLE,
                    failure=AtlasSourceFailure.UNAVAILABLE,
                    source="unknown",
                )
        return pool_control(
            census,
            holders,
            contract,
            origin,
            required=v4_market or census.has_pools,
            market_pool=(
                market.pool_locator.value if v4_market and market.pool_locator is not None else None
            ),
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
        reconciled = await self._reconcile(result, token_address)
        return self._holder_measurement(result, chain, token_address, contract, reconciled)

    async def _reconcile(
        self, result: HolderFactsSourceResult, token_address: str
    ) -> tuple[ExclusionReconciliation, ...]:
        """Read back what the provider excluded, where that can be proven.

        Only an explicitly reconcilable address (the zero address), only for a
        block-anchored holder state, and only at exactly the provider's own
        snapshot block — never the latest block as a stand-in. A missing block,
        a failed or malformed read, a result for another token, or any other
        excluded address leaves that exclusion unresolved; nothing is defaulted.
        """
        if (
            result.token_address != token_address
            or result.observation_basis != HolderObservationBasis.SOURCE_BLOCK
            or result.snapshot_block is None
        ):
            return ()
        block = result.snapshot_block
        reconciled: list[ExclusionReconciliation] = []
        for excluded in result.excluded_addresses:
            if excluded not in RECONCILABLE_EXCLUSIONS:
                continue
            try:
                balance = await self.contracts.balance_of(token_address, excluded, block)
            except Exception:
                balance = None
            if balance is None or type(balance) is not int or balance < 0:
                continue
            reconciled.append(
                ExclusionReconciliation(address=excluded, balance_raw=balance, block=block)
            )
        return tuple(reconciled)

    def _holder_measurement(
        self,
        result: HolderFactsSourceResult,
        chain: ChainSnapshot,
        token_address: str,
        contract: ContractFacts,
        reconciled: tuple[ExclusionReconciliation, ...] = (),
    ) -> HolderFacts:
        """Turn provider rows into the measured fact, or into an honest failure.

        A reconciled exclusion's on-chain balance joins the rows before anything
        is computed, so a large holding the provider filtered out counts in the
        raw top-N exactly like any other holder. Only exclusions still
        unresolved are passed on as exclusions.

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
        resolved = {item.address for item in reconciled}
        unresolved = tuple(item for item in result.excluded_addresses if item not in resolved)
        rows = result.rows + tuple(
            HolderSourceRow(address=item.address, balance_raw=item.balance_raw)
            for item in reconciled
            # A zero balance holds nothing and takes no rank.
            if item.balance_raw > 0
        )
        try:
            measured = concentration(rows, supply, result.completeness, unresolved)
        except HolderNormalizationError as error:
            return unusable(error.failure)
        return HolderFacts(
            status=Availability.AVAILABLE,
            source=result.source,
            # Anchored to what the source observed, never to when we fetched it.
            observed_at=result.snapshot_timestamp,
            observation_basis=result.observation_basis,
            completeness=result.completeness,
            excluded_addresses=result.excluded_addresses,
            reconciled_exclusions=reconciled,
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
class ChainRoutedSnapshotBuilder:
    """One builder per configured chain, chosen by the case's own market.

    Each builder's contract source is fixed to one chain, because the port reads
    "the" chain head and takes no chain argument. With several chains enabled
    the right builder is therefore the one keyed by the market's own chain and
    network — never whichever was configured first. A market on a chain without
    a builder is refused, and the chosen builder still refuses a source that
    answers for a different chain (`SOURCE_CHAIN_MISMATCH`).
    """

    builders: Mapping[tuple[str, str], AtlasSnapshotBuilder]

    async def build(
        self, trade_case_id: UUID, task_id: UUID, market: MarketIdentity
    ) -> AtlasOnchainSnapshot:
        builder = self.builders.get((market.chain, market.network))
        if builder is None:
            raise AtlasContextUnavailable("ONCHAIN_SOURCE_CHAIN_NOT_CONFIGURED")
        return await builder.build(trade_case_id, task_id, market)


@dataclass(frozen=True)
class AtlasContextReader:
    cases: TradeCaseIdentitySource
    builder: "SnapshotBuilderPort"

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


def _raw(value: int | None) -> str | None:
    return None if value is None else str(value)


def _ratio(value: Decimal | None) -> str | None:
    return None if value is None else canonical_decimal(value)


def funding_graph_document(facts: FundingGraphFacts) -> dict[str, object]:
    """The funding measurement in canonical form: every figure, a bounded edge sample."""
    return {
        "measurement": facts.measurement,
        "scope": facts.scope,
        "status": facts.status.value,
        "gap": None if facts.gap is None else facts.gap.value,
        "failure": None if facts.failure is None else facts.failure.value,
        "source": facts.source,
        "root_address": facts.root_address,
        "origin_source": facts.origin_source,
        "origin_verification": facts.origin_verification,
        "factory_address": facts.factory_address,
        "creation_block": facts.creation_block,
        "snapshot_block": facts.snapshot_block,
        "coverage": None if facts.coverage is None else facts.coverage.value,
        "requests_made": facts.requests_made,
        "transactions_read": facts.transactions_read,
        "direct_funding_tx_count": facts.direct_funding_tx_count,
        "unique_direct_funded_address_count": facts.unique_direct_funded_address_count,
        "total_direct_native_funding_raw": _raw(facts.total_direct_native_funding_raw),
        "first_funding_block": facts.first_funding_block,
        "last_funding_block": facts.last_funding_block,
        "edges_digest": facts.edges_digest,
        "sample_edges": [
            {
                "recipient": edge.recipient,
                "tx_hash": edge.tx_hash,
                "block_number": edge.block_number,
                "native_value_raw": str(edge.native_value_raw),
            }
            for edge in facts.sample_edges
        ],
        "holder_basis": facts.holder_basis.value,
        "observed_holder_count": facts.observed_holder_count,
        "creator_funded_observed_holder_count": facts.creator_funded_observed_holder_count,
        "creator_funded_observed_holder_fraction": _ratio(
            facts.creator_funded_observed_holder_fraction
        ),
        "creator_funded_observed_supply_fraction": _ratio(
            facts.creator_funded_observed_supply_fraction
        ),
        "creator_funded_observed_top10_count": facts.creator_funded_observed_top10_count,
        "creator_funded_observed_holders": list(facts.creator_funded_observed_holders),
    }


def pool_control_document(control: PoolControlFacts) -> dict[str, object]:
    """Pool control, every safety-relevant fact, in one bounded canonical form.

    Bounded by the fact model itself (pools and positions are capped there), so
    nothing here truncates. Integers that can exceed a JSON number travel as
    text, exactly as the holder figures do.
    """
    census = control.census
    return {
        "measurement": control.measurement,
        **_measurement(control.status, census.failure),
        "gap": None if control.gap is None else control.gap.value,
        "required": control.required,
        "census": {
            "status": census.status.value,
            "gap": None if census.gap is None else census.gap.value,
            "source": census.source,
            "chain_id": census.chain_id,
            "pool_manager": census.pool_manager,
            "position_managers": list(census.position_managers),
            "scan_from_block": census.scan_from_block,
            "scan_to_block": census.scan_to_block,
            "pool_manager_balance_raw": _raw(census.pool_manager_balance_raw),
        },
        "pools": [
            {
                "pool_id": pool.pool_id,
                "pool_manager": pool.pool_manager,
                "currency0": pool.currency0,
                "currency1": pool.currency1,
                "fee": pool.fee,
                "tick_spacing": pool.tick_spacing,
                "hook": pool.hook,
                "created_block": pool.created_block,
                "created_at": None if pool.created_at is None else pool.created_at.isoformat(),
                "initialized": pool.initialized,
                "sqrt_price_x96": _raw(pool.sqrt_price_x96),
                "tick": pool.tick,
                "hook_facts": None
                if pool.hook_facts is None
                else {
                    "code_present": pool.hook_facts.code_present,
                    "permissions": list(pool.hook_facts.permissions),
                    "owner": pool.hook_facts.owner,
                    "owner_status": pool.hook_facts.owner_status.value,
                    "owner_is_creator": pool.hook_facts.owner_is_creator,
                },
            }
            for pool in census.pools
        ],
        "positions": [
            {
                "kind": item.kind.value,
                "pool_id": item.pool_id,
                "owner_key": item.owner_key,
                "salt": item.salt,
                "position_manager": item.position_manager,
                "token_id": _raw(item.token_id),
                "tick_lower": item.tick_lower,
                "tick_upper": item.tick_upper,
                "liquidity": str(item.liquidity),
                "owner": item.owner,
                "owner_status": item.owner_status.value,
                "controlled_token_raw": str(item.controlled_token_raw),
                "owner_is_creator": item.owner_is_creator,
                # Only where control facts exist, so a position without them
                # keeps the digest it always had.
                **({} if item.control is None else {"control": _control(item.control)}),
            }
            for item in census.positions
        ],
        "total_supply_raw": _raw(control.total_supply_raw),
        "attributed_raw": _raw(control.attributed_raw),
        "unattributed_raw": _raw(control.unattributed_raw),
        "creator_controlled_raw": _raw(control.creator_controlled_raw),
        "pool_held_supply_fraction": _ratio(control.pool_held_supply_fraction),
        "attributable_pool_supply_fraction": _ratio(control.attributable_pool_supply_fraction),
        "creator_controlled_pool_supply_fraction": _ratio(
            control.creator_controlled_pool_supply_fraction
        ),
        "unattributed_pool_supply_fraction": _ratio(control.unattributed_pool_supply_fraction),
        "basis": None if control.basis is None else control.basis.value,
        "economic_top1_share": _ratio(control.economic_top1_share),
        "economic_top5_share": _ratio(control.economic_top5_share),
        "economic_top10_share": _ratio(control.economic_top10_share),
        "economic_top_holders": [
            {
                "holder": item.holder,
                "balance_raw": str(item.balance_raw),
                "share": canonical_decimal(item.share),
                "is_burn_address": item.is_burn_address,
            }
            for item in control.economic_top_holders
        ],
        # Control buckets and the floor, each only where it was established.
        **{
            key: value
            for key, value in {
                "permanently_locked_raw": _raw(control.permanently_locked_raw),
                "timelocked_raw": _raw(control.timelocked_raw),
                "releasable_raw": _raw(control.releasable_raw),
                "unknown_custody_raw": _raw(control.unknown_custody_raw),
                "economic_top10_floor": _ratio(control.economic_top10_floor),
            }.items()
            if value is not None
        },
    }


def _control(facts: PositionControlFacts) -> dict[str, object]:
    return {
        "position_owner": facts.position_owner,
        "owner_kind": facts.owner_kind.value,
        "control_state": facts.control_state.value,
        "controller": facts.controller,
        "unlock_block": facts.unlock_block,
        "proof_kind": facts.proof_kind.value,
        "proof_contract": facts.proof_contract,
        "proof_version": facts.proof_version,
        "owner_code_hash": facts.owner_code_hash,
        "completeness": facts.completeness.value,
        "refusal": None if facts.refusal is None else facts.refusal.value,
    }


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
            # What the provider filtered out before we saw it. Part of the
            # digest, so a provider silently changing its filtering changes the
            # fact fingerprint instead of passing unnoticed.
            "excluded_addresses": list(holders.excluded_addresses),
            # What was read back on-chain for those, and at which block. Only
            # present when something was, so every earlier digest is unchanged.
            **(
                {
                    "reconciled_exclusions": [
                        {
                            "address": item.address,
                            "balance_raw": str(item.balance_raw),
                            "block": item.block,
                            "method": item.method,
                        }
                        for item in holders.reconciled_exclusions
                    ]
                }
                if holders.reconciled_exclusions
                else {}
            ),
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
        # Only present where pool control was collected, so every snapshot
        # without it keeps the digest it always had.
        **(
            {}
            if snapshot.pool_control is None
            else {"pool_control": pool_control_document(snapshot.pool_control)}
        ),
        # Likewise only where a funding source is configured.
        **(
            {}
            if snapshot.funding_graph is None
            else {"funding_graph": funding_graph_document(snapshot.funding_graph)}
        ),
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
    """The spread policy judges: chain **block** time against the holder anchor.

    Deliberately not ``chain.observed_at``. Fetch times say when we ran, and a
    spread between two fetches would be near zero however old either fact is.

    The two operands are not epistemically equal when the holder anchor is
    ``RESPONSE_TIME``: one is an authoritative chain observation, the other a
    response receipt. The difference is then a bound on how far the pinned block
    lags the moment of the answer, not a source-to-source skew, and it cannot
    detect an indexer running behind. ``HolderObservationBasis`` on the fact is
    what says which reading applies; the number alone never does.
    """
    if snapshot.holders.observed_at is None:
        return None
    return abs(snapshot.chain.block_timestamp - snapshot.holders.observed_at)
