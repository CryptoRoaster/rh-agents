"""Optional bounded live reads. Never run in CI and never by default.

Two independent opt-ins, because the two paths have different costs:

* ``RH_AGENTS_LIVE_ONCHAIN_SMOKE=1`` reads the documented public Robinhood Chain
  RPC. No credential, no charge, read-only.
* ``RH_AGENTS_LIVE_HOLDER_SMOKE=1`` plus ``BLOCKSCOUT_API_KEY`` reads holder and
  creation data from the Blockscout PRO API. That consumes provider credits, so
  the presence of a key alone deliberately does not enable it.

Nothing here trades, signs, broadcasts or writes any state.
"""

import os

import pytest
from pydantic import SecretStr

from src.agents.atlas.rpc_source import RpcTokenContractSource
from src.agents.atlas.sources.blockscout import (
    BlockscoutConfig,
    BlockscoutContractOriginSource,
    BlockscoutHolderSource,
)
from src.core.config import Settings
from src.markets.models import Availability
from src.runtime.models import ChainConfig

# The documented public Robinhood Chain endpoints. Neither carries a credential,
# so neither is a secret; an operator may still point these elsewhere.
PUBLIC_RPC = os.environ.get("RH_PUBLIC_RPC_URL", "https://rpc.mainnet.chain.robinhood.com")
PUBLIC_WS = os.environ.get("RH_PUBLIC_WS_URL", "wss://rpc.mainnet.chain.robinhood.com")
# A contract verified to exist on Robinhood Chain mainnet during Phase 2E
# research. It is used as a read target only.
SMOKE_TOKEN = os.environ.get("RH_SMOKE_TOKEN", "0xb44b65190c47849acac56d86c144b699fd557777")

CHAIN_LIVE = os.environ.get("RH_AGENTS_LIVE_ONCHAIN_SMOKE") == "1"
HOLDER_LIVE = os.environ.get("RH_AGENTS_LIVE_HOLDER_SMOKE") == "1"
BLOCKSCOUT_KEY = os.environ.get("BLOCKSCOUT_API_KEY", "")

DATABASE = "postgresql+asyncpg://test_user@localhost/test_database"


def chain_config() -> ChainConfig:
    return ChainConfig(
        chain="robinhood",
        chain_id=4663,
        http_url=SecretStr(PUBLIC_RPC),
        ws_url=SecretStr(PUBLIC_WS),
        confirmations=12,
    )


@pytest.mark.skipif(not CHAIN_LIVE, reason="Live chain smoke is opt-in")
async def test_live_robinhood_contract_facts_are_readable():
    from src.runtime.rpc import EvmRpcClient

    settings = Settings(_env_file=None, database_url=DATABASE)
    client = EvmRpcClient(chain_config(), settings)
    try:
        source = RpcTokenContractSource(client=client, config=chain_config())
        snapshot = await source.chain_snapshot()
        assert snapshot.chain_id == 4663
        facts = await source.contract_facts(SMOKE_TOKEN, snapshot.block_number)
        assert facts.status == Availability.AVAILABLE
        assert facts.code_present is True
        assert facts.total_supply_raw is not None and facts.total_supply_raw > 0
    finally:
        await client.close()


@pytest.mark.skipif(
    not (HOLDER_LIVE and BLOCKSCOUT_KEY), reason="Live holder smoke is opt-in and needs a key"
)
async def test_live_blockscout_holder_and_creation_reads():
    config = BlockscoutConfig(
        base_url="https://api.blockscout.com",
        chain_id=4663,
        api_key=BLOCKSCOUT_KEY,
        max_pages=1,
    )
    holders = await BlockscoutHolderSource(config=config, chain="robinhood").holder_facts(
        "robinhood", SMOKE_TOKEN
    )
    assert holders.status == Availability.AVAILABLE
    assert holders.rows
    assert holders.snapshot_block is not None
    # One metadata read, one indexer head read, one holder page.
    assert holders.requests_made == 3

    origin = await BlockscoutContractOriginSource(config=config, chain="robinhood").origin_facts(
        "robinhood", SMOKE_TOKEN
    )
    assert origin.status in (Availability.AVAILABLE, Availability.UNAVAILABLE)
