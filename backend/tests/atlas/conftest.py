from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.agents.atlas.context import AtlasSnapshotBuilder, AtlasTaskInput
from src.agents.atlas.models import (
    AtlasOnchainSnapshot,
    ChainSnapshot,
    ContractFacts,
    HolderFacts,
    HolderShare,
    OriginFacts,
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


def chain_snapshot(now, *, chain="robinhood", chain_id=4663, block=1_000_000) -> ChainSnapshot:
    return ChainSnapshot(
        chain=chain,
        network="mainnet",
        chain_id=chain_id,
        block_number=block,
        observed_at=now,
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


def holder_facts(
    now,
    *,
    status=Availability.AVAILABLE,
    top1="0.05",
    top10="0.30",
    failure=None,
    observed_at=None,
) -> HolderFacts:
    if status != Availability.AVAILABLE:
        return HolderFacts(status=status, failure=failure, source="test-indexer")
    return HolderFacts(
        status=status,
        source="test-indexer",
        observed_at=observed_at or now,
        holder_count=4200,
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


def origin_facts(*, status=Availability.AVAILABLE, failure=None) -> OriginFacts:
    if status != Availability.AVAILABLE:
        return OriginFacts(status=status, failure=failure, source="test-origin")
    return OriginFacts(
        status=status,
        source="test-origin",
        creator_address="0x" + "e7" * 20,
        creation_block=900_000,
    )


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
    def __init__(self, facts) -> None:
        self._facts = facts

    async def holder_facts(self, chain, token_address):
        return self._facts


class StubOrigins:
    def __init__(self, facts) -> None:
        self._facts = facts

    async def origin_facts(self, chain, token_address):
        return self._facts


def builder_for(now, *, chain=None, contract=None, holders=None, origin=None):
    return AtlasSnapshotBuilder(
        contracts=StubContracts(chain or chain_snapshot(now), contract or contract_facts()),
        holders=StubHolders(holders if holders is not None else holder_facts(now)),
        origins=StubOrigins(origin if origin is not None else origin_facts()),
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
