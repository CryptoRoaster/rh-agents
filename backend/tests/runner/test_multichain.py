"""The PAPER path under `MARKET_CHAINS=robinhood,bsc`: each case on its own chain.

VECTOR's history and ATLAS's contract source are each bound to one chain at
construction. With both chains enabled the run builds one per chain and routes
every read by the case's own market identity (chain and network) — never by
whichever chain was configured first. Unknown or mis-wired chains fail closed.
"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agents.atlas.context import (
    AtlasContextUnavailable,
    AtlasSnapshotBuilder,
    ChainRoutedSnapshotBuilder,
)
from src.core.clock import FixedClock
from src.markets.history import ChainRoutedHistory, MarketHistoryUnavailable
from src.runner.composition import (
    ChainContractSources,
    RunnerPorts,
    atlas_builder,
    ports_from_settings,
)
from tests.atlas.conftest import (
    StubContracts,
    StubHolders,
    StubOrigins,
    chain_snapshot,
    contract_facts,
    holder_source_result,
    market_identity,
    origin_facts,
)
from tests.runner.conftest import runner_settings

NOW = datetime(2026, 9, 28, 18, tzinfo=UTC)
RH = market_identity("robinhood")
BSC = market_identity("bsc")
BOTH_RPC = {
    "evm_runtime_enabled": True,
    "rh_chain_enabled": True,
    "bsc_chain_enabled": True,
    "rh_rpc_http_url": "https://rh.invalid",
    "rh_rpc_ws_url": "wss://rh.invalid",
    "bsc_rpc_http_url": "https://bsc.invalid",
    "bsc_rpc_ws_url": "wss://bsc.invalid",
}


class SpyHistory:
    """A per-chain history source that records which identities reached it."""

    def __init__(self, chain: str) -> None:
        self.chain = chain
        self.seen: list[str] = []

    async def history(self, identity, *, timeframe, aggregate, bars):
        self.seen.append(identity.chain)
        if identity.chain != self.chain:
            raise AssertionError("cross-chain read")
        return f"{self.chain}-history"


def contracts_for(chain: str, chain_id: int) -> StubContracts:
    return StubContracts(chain_snapshot(NOW, chain=chain, chain_id=chain_id), contract_facts())


def builder(contracts: StubContracts) -> AtlasSnapshotBuilder:
    return AtlasSnapshotBuilder(
        contracts=contracts,
        holders=StubHolders(holder_source_result(NOW)),
        origins=StubOrigins(origin_facts()),
        clock=FixedClock(NOW),
    )


def routed_builder(**overrides) -> ChainRoutedSnapshotBuilder:
    builders = {
        ("robinhood", "mainnet"): builder(contracts_for("robinhood", 4663)),
        ("bsc", "mainnet"): builder(contracts_for("bsc", 56)),
    }
    return ChainRoutedSnapshotBuilder({**builders, **overrides})


# ------------------------------------------------------- VECTOR history


def routed_history():
    spies = {"robinhood": SpyHistory("robinhood"), "bsc": SpyHistory("bsc")}
    history = ChainRoutedHistory({(name, "mainnet"): spy for name, spy in spies.items()})
    return history, spies


async def read(history, identity):
    return await history.history(identity, timeframe="hour", aggregate=1, bars=48)


async def test_a_robinhood_case_reads_robinhood_history():
    history, spies = routed_history()
    assert await read(history, RH) == "robinhood-history"
    assert (spies["robinhood"].seen, spies["bsc"].seen) == (["robinhood"], [])


async def test_a_bsc_case_reads_bsc_history():
    history, spies = routed_history()
    assert await read(history, BSC) == "bsc-history"
    assert (spies["robinhood"].seen, spies["bsc"].seen) == ([], ["bsc"])


async def test_both_chains_in_one_process_never_cross():
    history, spies = routed_history()
    results = await asyncio.gather(*(read(history, item) for item in (RH, BSC, RH, BSC)))
    assert results == ["robinhood-history", "bsc-history"] * 2
    assert spies["robinhood"].seen == ["robinhood", "robinhood"]
    assert spies["bsc"].seen == ["bsc", "bsc"]


@pytest.mark.parametrize(
    "identity,code",
    [
        (RH.model_copy(update={"network": "testnet"}), "MARKET_HISTORY_CHAIN_NOT_CONFIGURED"),
        ("robinhood:mainnet:pair", "MARKET_HISTORY_IDENTITY_INVALID"),
    ],
)
async def test_an_unknown_or_malformed_market_fails_closed(identity, code):
    history, spies = routed_history()
    with pytest.raises(MarketHistoryUnavailable) as caught:
        await read(history, identity)
    assert caught.value.reason_code == code
    assert spies["robinhood"].seen == spies["bsc"].seen == []


async def test_a_single_configured_chain_refuses_the_other():
    only = ChainRoutedHistory({("robinhood", "mainnet"): SpyHistory("robinhood")})
    with pytest.raises(MarketHistoryUnavailable) as caught:
        await read(only, BSC)
    assert caught.value.reason_code == "MARKET_HISTORY_CHAIN_NOT_CONFIGURED"


# ------------------------------------------------------- ATLAS on-chain source


async def test_a_robinhood_case_reads_the_robinhood_chain():
    snapshot = await routed_builder().build(uuid4(), uuid4(), RH)
    assert (snapshot.chain.chain, snapshot.chain.chain_id) == ("robinhood", 4663)


async def test_a_bsc_case_reads_the_bsc_chain():
    snapshot = await routed_builder().build(uuid4(), uuid4(), BSC)
    assert (snapshot.chain.chain, snapshot.chain.chain_id) == ("bsc", 56)


async def test_both_chains_concurrently_each_get_their_own_evidence():
    routed = routed_builder()
    snapshots = await asyncio.gather(
        *(routed.build(uuid4(), uuid4(), item) for item in (RH, BSC, BSC, RH))
    )
    assert [item.chain.chain_id for item in snapshots] == [4663, 56, 56, 4663]
    assert [item.market.chain for item in snapshots] == ["robinhood", "bsc", "bsc", "robinhood"]


async def test_a_chain_without_a_source_fails_closed():
    only = ChainRoutedSnapshotBuilder(
        {("robinhood", "mainnet"): builder(contracts_for("robinhood", 4663))}
    )
    with pytest.raises(AtlasContextUnavailable) as caught:
        await only.build(uuid4(), uuid4(), BSC)
    assert caught.value.reason_code == "ONCHAIN_SOURCE_CHAIN_NOT_CONFIGURED"
    with pytest.raises(AtlasContextUnavailable) as caught:
        await only.build(uuid4(), uuid4(), RH.model_copy(update={"network": "testnet"}))
    assert caught.value.reason_code == "ONCHAIN_SOURCE_CHAIN_NOT_CONFIGURED"


async def test_a_miswired_source_is_refused_not_trusted():
    """A BSC slot answering with Robinhood's chain is cross-chain evidence: refused."""
    miswired = ChainRoutedSnapshotBuilder(
        {("bsc", "mainnet"): builder(contracts_for("robinhood", 4663))}
    )
    with pytest.raises(AtlasContextUnavailable) as caught:
        await miswired.build(uuid4(), uuid4(), BSC)
    assert caught.value.reason_code == "SOURCE_CHAIN_MISMATCH"


# ------------------------------------------------------- composition


@pytest.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        yield async_sessionmaker(engine)
    finally:
        await engine.dispose()


async def composed(settings, sessions):
    ports = ports_from_settings(settings, sessions, clock=FixedClock(NOW))
    try:
        return ports
    finally:
        for close in ports.closers:
            await close()


async def test_both_chains_configured_build_a_source_per_chain(sessions):
    settings = runner_settings(
        market_provider="geckoterminal",
        market_chains="robinhood,bsc",
        vector_history_provider="geckoterminal",
        **BOTH_RPC,
    )
    ports = await composed(settings, sessions)
    assert isinstance(ports.history, ChainRoutedHistory)
    assert set(ports.history.sources) == {("robinhood", "mainnet"), ("bsc", "mainnet")}
    assert isinstance(ports.onchain, ChainContractSources)
    assert set(ports.onchain.sources) == {("robinhood", "mainnet"), ("bsc", "mainnet")}
    # No ambiguity code is produced any more.
    assert "AMBIGUOUS" not in ports.history_unavailable + ports.onchain_unavailable
    routed = atlas_builder(settings, ports, FixedClock(NOW))
    assert isinstance(routed, ChainRoutedSnapshotBuilder)
    assert set(routed.builders) == set(ports.onchain.sources)


async def test_a_single_supplied_source_stays_a_single_builder(sessions):
    settings = runner_settings()
    ports = RunnerPorts(onchain=contracts_for("robinhood", 4663))
    single = atlas_builder(settings, ports, FixedClock(NOW))
    assert isinstance(single, AtlasSnapshotBuilder)
    with pytest.raises(AtlasContextUnavailable) as caught:
        await single.build(uuid4(), uuid4(), BSC)
    assert caught.value.reason_code == "SOURCE_CHAIN_MISMATCH"


class Cases:
    """Two cases, one per chain, read the way ATLAS's context reader reads them."""

    def __init__(self, markets) -> None:
        self.markets = markets

    async def get_trade_case(self, trade_case_id):
        return type("Case", (), {"market": self.markets[trade_case_id]})()

    async def evidence(self, trade_case_id):
        return ()


async def test_each_case_s_own_market_selects_its_chain_through_the_reader():
    from src.agents.atlas.context import AtlasContextReader

    rh_case, bsc_case = uuid4(), uuid4()
    reader = AtlasContextReader(
        cases=Cases({rh_case: RH, bsc_case: BSC}),  # type: ignore[arg-type]
        builder=routed_builder(),
    )
    rh, bsc = await asyncio.gather(
        reader.onchain_context(rh_case, uuid4()), reader.onchain_context(bsc_case, uuid4())
    )
    assert (rh.snapshot.chain.chain, rh.snapshot.trade_case_id) == ("robinhood", rh_case)
    assert (bsc.snapshot.chain.chain, bsc.snapshot.trade_case_id) == ("bsc", bsc_case)
