from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.agents.atlas.context import AtlasSnapshotBuilder, AtlasTaskInput
from src.agents.atlas.models import (
    AtlasOnchainSnapshot,
    ChainSnapshot,
    ContractFacts,
    HolderCompleteness,
    HolderFacts,
    HolderFactsSourceResult,
    HolderObservationBasis,
    HolderShare,
    HolderSourceRow,
    OriginFacts,
    OriginVerification,
    ProxyObservation,
)
from src.core.clock import FixedClock
from src.markets.models import Availability, MarketIdentity

# Real 20-byte addresses: ATLAS refuses anything that is not one, so the market
# fixtures used elsewhere (0xfixture-weth) deliberately cannot be used here.
TOKEN = "0x" + "a1" * 20
QUOTE = "0x" + "b2" * 20
WHALE = "0x" + "c3" * 20
BURN = "0x" + "0" * 36 + "dead"
ADMIN = "0x" + "d4" * 20
from tests.worker.conftest import worker_db as worker_db  # noqa: E402, F401


def market_identity(chain: str = "robinhood", network: str = "mainnet") -> MarketIdentity:
    return MarketIdentity(
        provider="geckoterminal",
        chain=chain,
        network=network,
        pair_id=f"{chain}:{network}:contract_address:{'0x' + 'e5' * 20}",
        base_asset_id=f"{chain}:{network}:{TOKEN}",
        quote_asset_id=f"{chain}:{network}:{QUOTE}",
        venue="uniswap-v3",
        is_fixture=False,
    )


def chain_snapshot(
    now, *, chain="robinhood", chain_id=4663, block=1_000_000, block_timestamp=None, fetched_at=None
) -> ChainSnapshot:
    return ChainSnapshot(
        chain=chain,
        network="mainnet",
        chain_id=chain_id,
        block_number=block,
        block_timestamp=block_timestamp or now,
        observed_at=fetched_at or now,
        source="evm-rpc",
    )


def contract_facts(
    *,
    status=Availability.AVAILABLE,
    code_present=True,
    decimals=18,
    total_supply_raw=1_000_000 * 10**18,
    proxy=ProxyObservation.EIP1967_SLOTS_EMPTY,
    admin=None,
    failure=None,
    block=1_000_000,
) -> ContractFacts:
    if status != Availability.AVAILABLE:
        return ContractFacts(status=status, failure=failure, source="evm-rpc")
    return ContractFacts(
        status=status,
        source="evm-rpc",
        observed_block=block,
        code_present=code_present,
        decimals=decimals,
        total_supply_raw=total_supply_raw,
        proxy=proxy,
        implementation_address=None if admin is None else "0x" + "f6" * 20,
        admin_address=admin,
    )


TOTAL_SUPPLY = 1_000_000 * 10**18


def holder_facts(
    now,
    *,
    status=Availability.AVAILABLE,
    top1="0.05",
    top10="0.30",
    failure=None,
    observed_at=None,
    completeness=HolderCompleteness.TOP_N_ONLY,
    observation_basis=HolderObservationBasis.SOURCE_BLOCK,
    snapshot_block=1_000_000,
    holder_block_delta=0,
) -> HolderFacts:
    if status != Availability.AVAILABLE:
        return HolderFacts(status=status, failure=failure, source="test-indexer")
    return HolderFacts(
        status=status,
        source="test-indexer",
        observed_at=observed_at or now,
        observation_basis=observation_basis,
        completeness=completeness,
        snapshot_block=snapshot_block,
        holder_block_delta=holder_block_delta,
        holder_count=4200,
        total_supply_raw=TOTAL_SUPPLY,
        top_holders=(
            HolderShare(
                address=WHALE,
                balance_raw=50_000 * 10**18,
                share=Decimal(top1),
                is_burn_address=False,
            ),
            HolderShare(
                address=BURN,
                balance_raw=10_000 * 10**18,
                share=Decimal("0.01"),
                is_burn_address=True,
            ),
        ),
        top1_share=Decimal(top1),
        top5_share=Decimal(top10),
        top10_share=Decimal(top10),
        top10_share_excluding_burn=Decimal(top10),
    )


def holder_rows(count=12, *, top_balance=50_000 * 10**18, step=1_000 * 10**18):
    """A descending, duplicate-free row set with one canonical burn holder."""
    rows = [
        HolderSourceRow(
            address="0x" + f"{index + 1:02x}" * 20,
            balance_raw=top_balance - index * step,
            is_contract=False,
        )
        for index in range(count - 1)
    ]
    rows.append(HolderSourceRow(address=BURN, balance_raw=1_000 * 10**18))
    return tuple(sorted(rows, key=lambda row: (-row.balance_raw, row.address)))


def holder_source_result(
    now,
    *,
    status=Availability.AVAILABLE,
    failure=None,
    rows=None,
    completeness=HolderCompleteness.TOP_N_ONLY,
    observation_basis=HolderObservationBasis.SOURCE_BLOCK,
    snapshot_block=1_000_000,
    snapshot_timestamp=None,
    chain="robinhood",
    token_address=TOKEN,
    holder_count=4200,
    provider_total_supply_raw=TOTAL_SUPPLY,
) -> HolderFactsSourceResult:
    if status != Availability.AVAILABLE:
        return HolderFactsSourceResult(status=status, failure=failure, source="test-indexer")
    return HolderFactsSourceResult(
        status=status,
        source="test-indexer",
        chain=chain,
        token_address=token_address,
        rows=holder_rows() if rows is None else rows,
        completeness=completeness,
        observation_basis=observation_basis,
        snapshot_block=snapshot_block,
        snapshot_timestamp=snapshot_timestamp or now,
        holder_count=holder_count,
        provider_total_supply_raw=provider_total_supply_raw,
        requests_made=3,
    )


CREATOR = "0x" + "e7" * 20
CREATION_TX = "0x" + "11" * 32


def origin_facts(
    *,
    status=Availability.AVAILABLE,
    failure=None,
    creation_tx_hash=CREATION_TX,
    verification=OriginVerification.NOT_ATTEMPTED,
) -> OriginFacts:
    if status != Availability.AVAILABLE:
        return OriginFacts(status=status, failure=failure, source="test-origin")
    return OriginFacts(
        status=status,
        source="test-origin",
        creator_address=CREATOR,
        creation_block=900_000,
        creation_tx_hash=creation_tx_hash,
        verification=verification,
    )


class StubVerifier:
    """Chain-side confirmation stub. ``created`` None means the check failed."""

    def __init__(self, created=TOKEN, creator_is_contract=False) -> None:
        self._created = created
        self._creator_is_contract = creator_is_contract

    async def creation_receipt_contract(self, tx_hash):
        return self._created

    async def is_contract(self, address, block):
        return self._creator_is_contract


def snapshot(
    now,
    *,
    trade_case_id=None,
    task_id=None,
    market=None,
    chain=None,
    contract=None,
    holders=None,
    origin=None,
    collected_at=None,
) -> AtlasOnchainSnapshot:
    return AtlasOnchainSnapshot(
        trade_case_id=trade_case_id or uuid4(),
        task_id=task_id or uuid4(),
        market=market or market_identity(),
        token_address=TOKEN,
        chain=chain or chain_snapshot(now),
        contract=contract or contract_facts(),
        holders=holders if holders is not None else holder_facts(now),
        origin=origin if origin is not None else origin_facts(),
        collected_at=collected_at or now,
    )


class StubContracts:
    def __init__(self, chain, contract) -> None:
        self._chain = chain
        self._contract = contract

    async def chain_snapshot(self):
        return self._chain

    async def contract_facts(self, token_address, block):
        return self._contract


class StubHolders:
    def __init__(self, result) -> None:
        self._result = result

    async def holder_facts(self, chain, token_address):
        return self._result


class StubOrigins:
    def __init__(self, facts) -> None:
        self._facts = facts

    async def origin_facts(self, chain, token_address):
        return self._facts


def builder_for(now, *, chain=None, contract=None, holders=None, origin=None, verifier=None):
    return AtlasSnapshotBuilder(
        contracts=StubContracts(chain or chain_snapshot(now), contract or contract_facts()),
        holders=StubHolders(holders if holders is not None else holder_source_result(now)),
        origins=StubOrigins(origin if origin is not None else origin_facts()),
        verifier=verifier,
        clock=FixedClock(now),
    )


def task_input(now, **kwargs) -> AtlasTaskInput:
    return AtlasTaskInput(snapshot=snapshot(now, **kwargs))


@pytest.fixture
def atlas_snapshot(now):
    return snapshot(now)


@pytest.fixture
def atlas_input(now):
    return task_input(now)


VALIDITY = timedelta(minutes=10)
