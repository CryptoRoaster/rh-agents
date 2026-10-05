"""Custody adapters against the real official code, and everything that only looks like it.

A position NFT held by a contract is PERMANENTLY_LOCKED, TIMELOCKED or
RELEASABLE only when that contract's deployed code is exactly a pinned official
build, its immutables bind it to the verified V4 deployment, and -- for a
FeeSplitter -- it is one of the launcher's own deployments on this chain.
Anything else is UNKNOWN_CONTRACT_CUSTODY, never a lock.
"""

import pytest

from src.agents.atlas.v4.census import PoolControlChainRefused
from src.agents.atlas.v4.control import (
    ControlCompleteness,
    ControlProofKind,
    CustodyChainRefused,
    CustodyQuery,
    CustodyRefusal,
    PositionControlFacts,
    PositionControlState,
    PositionOwnerKind,
)
from src.agents.atlas.v4.custody.resolver import (
    BEACON_SLOT,
    IMPLEMENTATION_SLOT,
    resolve_owner,
)
from src.agents.atlas.v4.custody.template import code_keccak, match_template
from src.agents.atlas.v4.custody.uniswap_launcher import (
    FEE_SPLITTER_TEMPLATE,
    REGISTRIES,
    TIMELOCKED_RECIPIENT_TEMPLATE,
    FeeSplitterAdapter,
)
from src.agents.atlas.v4.keccak import keccak256
from src.agents.atlas.v4.protocol import V4_DEPLOYMENTS
from tests.atlas.conftest import CREATOR, TOKEN
from tests.atlas.v4.chain import (
    CHAIN_ID,
    HEAD_BLOCK,
    POOL_MANAGER,
    POSITION_MANAGER,
    SECOND_POSITION_MANAGER,
    FakeV4Chain,
)
from tests.atlas.v4.custody import (
    OFFICIAL_SPLITTER,
    OLDER_OFFICIAL_SPLITTER,
    OPERATOR,
    SECOND_OFFICIAL_SPLITTER,
    TIMELOCK_RECIPIENT,
    UNLISTED_SPLITTER,
    fee_splitter_code,
    minimal_proxy_to,
    template_code,
    timelocked_code,
)
from tests.atlas.v4.scenarios import build, census_for, traded_pool

TOKEN_ID = 5


class NoReads:
    """Custody reads for adapters that must decide from the code alone."""

    def __init__(self, slots: dict[tuple[str, str], int] | None = None) -> None:
        self.slots = slots or {}
        self.reads = 0

    async def code(self, address: str) -> str:
        raise AssertionError("the resolver already read the owner's code")

    async def storage(self, address: str, slot: str) -> str:
        self.reads += 1
        return "0x" + format(self.slots.get((address, slot), 0), "064x")

    async def call(self, address: str, selector: str) -> str:
        raise AssertionError("no adapter needs a call")


def query(owner: str, code: str, **changes: object) -> CustodyQuery:
    values: dict[str, object] = {
        "chain": "robinhood",
        "chain_id": CHAIN_ID,
        "block": HEAD_BLOCK,
        "pool_manager": POOL_MANAGER,
        "position_manager": POSITION_MANAGER,
        "token_id": TOKEN_ID,
        "pool_id": "0x" + "33" * 32,
        "owner": owner,
        "owner_code": code,
        "owner_code_hash": code_keccak(code),
    }
    values.update(changes)
    return CustodyQuery(**values)  # type: ignore[arg-type]


async def resolve(owner: str, code: str, reads: NoReads | None = None, **changes: object):
    return await resolve_owner(reads or NoReads(), query(owner, code, **changes))


# ---------------------------------------------------------------- the pins


@pytest.mark.parametrize(
    ("template", "name"),
    [
        (FEE_SPLITTER_TEMPLATE, "FeeSplitter"),
        (TIMELOCKED_RECIPIENT_TEMPLATE, "TimelockedPositionRecipient"),
    ],
)
def test_the_pinned_hash_is_the_official_build_with_immutables_zeroed(template, name) -> None:
    code = template_code(name)
    assert len(code) == template.length
    assert "0x" + keccak256(code).hex() == template.masked_keccak
    assert template.source_commit == "7ea523c9d75a51cb2f497be5e49bacdaeb80a342"


def test_the_official_feesplitter_binds_the_configured_v4_deployment() -> None:
    deployment = V4_DEPLOYMENTS[("robinhood", 4663)]
    for item in REGISTRIES["robinhood"].fee_splitters:
        assert item.position_manager in deployment.position_managers
        assert item.pool_manager == deployment.pool_manager


def test_the_robinhood_runtime_code_is_the_pinned_build_filled_with_its_managers() -> None:
    """The code every Robinhood v3.3.0 FeeSplitter must carry, byte for byte."""
    assert code_keccak(fee_splitter_code()) == (
        "0x8238e5106b3a895514083110d1f3b4e51be61148604f35113719af56ae325f42"
    )


def test_a_template_needs_one_value_per_immutable() -> None:
    code = bytearray(bytes.fromhex(fee_splitter_code()[2:]))
    second = FEE_SPLITTER_TEMPLATE.immutables["positionManager"][1]
    code[second + 31] ^= 1
    assert match_template("0x" + code.hex(), FEE_SPLITTER_TEMPLATE) is None


# ------------------------------------------------------------- FeeSplitter


@pytest.mark.parametrize(
    ("owner", "version"),
    [
        (OFFICIAL_SPLITTER, "v3.3.0"),
        (SECOND_OFFICIAL_SPLITTER, "v3.3.0"),
        (OLDER_OFFICIAL_SPLITTER, "v3.2.0"),
    ],
)
async def test_an_official_feesplitter_is_permanently_locked(owner, version) -> None:
    facts = await resolve(owner, fee_splitter_code())
    assert facts.control_state is PositionControlState.PERMANENTLY_LOCKED
    assert facts.controller is None
    assert facts.owner_kind is PositionOwnerKind.CONTRACT
    assert facts.proof_kind is ControlProofKind.VERIFIED_CUSTODY_CODE
    assert facts.proof_contract == "uniswap-liquidity-launcher:FeeSplitter"
    assert facts.proof_version == version
    assert facts.completeness is ControlCompleteness.VERIFIED
    assert facts.position_owner == owner


async def test_a_fake_feesplitter_with_the_same_interface_is_unknown() -> None:
    """Same length, same selectors, one byte different: not the audited code."""
    code = bytearray(bytes.fromhex(fee_splitter_code()[2:]))
    code[100] ^= 0xFF
    facts = await resolve(OFFICIAL_SPLITTER, "0x" + code.hex())
    assert facts.control_state is PositionControlState.UNKNOWN_CONTRACT_CUSTODY
    assert facts.refusal is CustodyRefusal.CODE_NOT_RECOGNISED
    assert facts.completeness is ControlCompleteness.UNRECOGNISED


async def test_a_feesplitter_with_a_hidden_withdrawal_path_is_unknown() -> None:
    """Extra code -- a transfer, a decrease, an arbitrary call -- changes the bytes."""
    facts = await resolve(OFFICIAL_SPLITTER, fee_splitter_code() + "5b6000ff")
    assert facts.control_state is PositionControlState.UNKNOWN_CONTRACT_CUSTODY
    assert facts.refusal is CustodyRefusal.CODE_NOT_RECOGNISED


async def test_official_code_at_an_unlisted_address_is_not_an_official_deployment() -> None:
    facts = await resolve(UNLISTED_SPLITTER, fee_splitter_code())
    assert facts.control_state is PositionControlState.UNKNOWN_CONTRACT_CUSTODY
    assert facts.refusal is CustodyRefusal.DEPLOYMENT_MISMATCH
    assert facts.completeness is ControlCompleteness.REFUTED
    assert facts.proof_contract == "uniswap-liquidity-launcher:FeeSplitter"


async def test_a_feesplitter_bound_to_another_position_manager_is_unknown() -> None:
    code = fee_splitter_code(position_manager=SECOND_POSITION_MANAGER)
    facts = await resolve(OFFICIAL_SPLITTER, code)
    assert facts.control_state is PositionControlState.UNKNOWN_CONTRACT_CUSTODY
    assert facts.refusal is CustodyRefusal.POSITION_MANAGER_MISMATCH


async def test_a_feesplitter_asked_about_another_managers_position_is_unknown() -> None:
    facts = await resolve(
        OFFICIAL_SPLITTER, fee_splitter_code(), position_manager=SECOND_POSITION_MANAGER
    )
    assert facts.refusal is CustodyRefusal.POSITION_MANAGER_MISMATCH


async def test_a_feesplitter_bound_to_another_pool_manager_is_unknown() -> None:
    code = fee_splitter_code(pool_manager="0x" + "99" * 20)
    facts = await resolve(OFFICIAL_SPLITTER, code)
    assert facts.refusal is CustodyRefusal.POSITION_MANAGER_MISMATCH


async def test_a_feesplitter_on_an_unsupported_chain_is_unknown() -> None:
    facts = await resolve(OFFICIAL_SPLITTER, fee_splitter_code(), chain="bsc", chain_id=56)
    assert facts.control_state is PositionControlState.UNKNOWN_CONTRACT_CUSTODY
    assert facts.refusal is CustodyRefusal.CHAIN_NOT_SUPPORTED


async def test_a_registry_chain_answering_for_another_chain_id_is_a_hard_refusal() -> None:
    with pytest.raises(CustodyChainRefused):
        await resolve(OFFICIAL_SPLITTER, fee_splitter_code(), chain_id=1)


async def test_an_adapter_without_a_registry_for_the_chain_proves_nothing() -> None:
    adapter = FeeSplitterAdapter(registries={})
    verdict = await adapter.verify(NoReads(), query(OFFICIAL_SPLITTER, fee_splitter_code()))
    assert verdict is not None
    assert verdict.state is PositionControlState.UNKNOWN_CONTRACT_CUSTODY
    assert verdict.refusal is CustodyRefusal.CHAIN_NOT_SUPPORTED


# ------------------------------------------------------------------ proxies


async def test_a_minimal_proxy_in_front_of_the_official_splitter_is_unknown() -> None:
    facts = await resolve(UNLISTED_SPLITTER, minimal_proxy_to(OFFICIAL_SPLITTER))
    assert facts.control_state is PositionControlState.UNKNOWN_CONTRACT_CUSTODY
    assert facts.refusal is CustodyRefusal.PROXY_INDIRECTION


@pytest.mark.parametrize("slot", [IMPLEMENTATION_SLOT, BEACON_SLOT])
async def test_an_upgradeable_proxy_is_unknown(slot) -> None:
    proxy = "0x" + "3c" * 20
    reads = NoReads({(proxy, slot): int(OFFICIAL_SPLITTER, 16)})
    facts = await resolve(proxy, "0x6080604052348015600f57600080fd5b50", reads)
    assert facts.control_state is PositionControlState.UNKNOWN_CONTRACT_CUSTODY
    assert facts.refusal is CustodyRefusal.PROXY_INDIRECTION


async def test_an_unknown_contract_is_unknown_custody_not_a_guess() -> None:
    reads = NoReads()
    facts = await resolve("0x" + "3d" * 20, "0x6080604052", reads)
    assert facts.control_state is PositionControlState.UNKNOWN_CONTRACT_CUSTODY
    assert facts.refusal is CustodyRefusal.CODE_NOT_RECOGNISED
    assert facts.controller is None and facts.proof_kind is ControlProofKind.NONE
    assert reads.reads == 2  # both proxy slots, and nothing else


# ------------------------------------------------- TimelockedPositionRecipient


async def test_a_timelock_not_yet_reached_is_timelocked() -> None:
    facts = await resolve(TIMELOCK_RECIPIENT, timelocked_code(timelock=HEAD_BLOCK + 1))
    assert facts.control_state is PositionControlState.TIMELOCKED
    assert facts.unlock_block == HEAD_BLOCK + 1
    assert facts.controller is None
    assert facts.proof_contract == "uniswap-liquidity-launcher:TimelockedPositionRecipient"
    assert facts.unlock_timestamp is None


@pytest.mark.parametrize("timelock", [HEAD_BLOCK, HEAD_BLOCK - 1, 1])
async def test_a_passed_timelock_is_releasable_to_its_operator(timelock) -> None:
    facts = await resolve(TIMELOCK_RECIPIENT, timelocked_code(timelock=timelock))
    assert facts.control_state is PositionControlState.RELEASABLE
    assert facts.controller == OPERATOR
    assert facts.unlock_block == timelock


async def test_a_timelock_counted_in_another_block_clock_is_unknown() -> None:
    facts = await resolve(TIMELOCK_RECIPIENT, timelocked_code(timelock=1, use_arb_sys=0))
    assert facts.control_state is PositionControlState.UNKNOWN_CONTRACT_CUSTODY
    assert facts.refusal is CustodyRefusal.BLOCK_CLOCK_UNSUPPORTED


async def test_a_timelock_for_another_position_manager_is_unknown() -> None:
    code = timelocked_code(timelock=1, position_manager=SECOND_POSITION_MANAGER)
    facts = await resolve(TIMELOCK_RECIPIENT, code)
    assert facts.refusal is CustodyRefusal.POSITION_MANAGER_MISMATCH


async def test_a_timelock_without_a_real_operator_is_unknown() -> None:
    facts = await resolve(TIMELOCK_RECIPIENT, timelocked_code(timelock=1, operator="0x" + "0" * 40))
    assert facts.refusal is CustodyRefusal.IMMUTABLE_INVALID


# ---------------------------------------------------------------- accounts


@pytest.mark.parametrize("code", ["0x", "0xef0100" + "11" * 20])
async def test_an_account_has_direct_control(code) -> None:
    facts = await resolve(CREATOR, code)
    assert facts.control_state is PositionControlState.DIRECT_CONTROL
    assert facts.controller == CREATOR
    assert facts.owner_kind is PositionOwnerKind.EXTERNALLY_OWNED


# ------------------------------------------------------------ fact invariants


def test_a_lock_cannot_be_asserted_without_verified_code() -> None:
    with pytest.raises(ValueError, match="verified custody code"):
        PositionControlFacts(
            position_manager=POSITION_MANAGER,
            token_id=1,
            position_owner=OFFICIAL_SPLITTER,
            owner_kind=PositionOwnerKind.CONTRACT,
            control_state=PositionControlState.PERMANENTLY_LOCKED,
            proof_kind=ControlProofKind.NONE,
            completeness=ControlCompleteness.UNRECOGNISED,
        )


def test_a_contract_is_never_in_direct_control() -> None:
    with pytest.raises(ValueError):
        PositionControlFacts(
            position_manager=POSITION_MANAGER,
            token_id=1,
            position_owner=OFFICIAL_SPLITTER,
            owner_kind=PositionOwnerKind.CONTRACT,
            control_state=PositionControlState.DIRECT_CONTROL,
            controller=OFFICIAL_SPLITTER,
            proof_kind=ControlProofKind.EXTERNALLY_OWNED_ACCOUNT,
            completeness=ControlCompleteness.VERIFIED,
        )


# ------------------------------------------------------------- in the census


async def test_the_census_resolves_each_owner_once_within_its_budget(now) -> None:
    chain = FakeV4Chain(token=TOKEN)
    chain.codes[OFFICIAL_SPLITTER] = fee_splitter_code()
    pool = chain.add_pool(traded_pool())
    for token_id in (1, 2, 3):
        chain.mint(pool, OFFICIAL_SPLITTER, token_id, -160_100, 198_050, 10**20)
    asked: list[str] = []
    read_code = chain.code

    async def counted(address: str, block: int) -> str:
        asked.append(address)
        return await read_code(address, block)

    chain.code = counted  # type: ignore[method-assign]
    snapshot = await build(now, chain)
    positions = snapshot.pool_control.census.positions
    assert len(positions) == 3
    assert {item.control.control_state for item in positions} == {
        PositionControlState.PERMANENTLY_LOCKED
    }
    assert asked.count(OFFICIAL_SPLITTER) == 1


async def test_a_wrong_chain_registry_stops_the_census_hard(now) -> None:
    from src.agents.atlas.v4.custody.uniswap_launcher import (
        ChainCustodyRegistry,
        TimelockedRecipientAdapter,
    )
    from tests.atlas.conftest import chain_snapshot

    chain = FakeV4Chain(token=TOKEN)
    chain.codes[OFFICIAL_SPLITTER] = fee_splitter_code()
    pool = chain.add_pool(traded_pool())
    chain.mint(pool, OFFICIAL_SPLITTER, 1, -160_100, 198_050, 10**20)
    elsewhere = {
        "robinhood": ChainCustodyRegistry(
            chain="robinhood", chain_id=1, fee_splitters=(), arbsys_block_clock=True
        )
    }
    census = census_for(chain)
    from dataclasses import replace

    census = replace(
        census,
        custody=(
            FeeSplitterAdapter(registries=elsewhere),
            TimelockedRecipientAdapter(registries=elsewhere),
        ),
    )
    from tests.atlas.v4.chain import CREATED_BLOCK

    with pytest.raises(PoolControlChainRefused):
        await census.census(chain_snapshot(now, block=HEAD_BLOCK), TOKEN, CREATED_BLOCK)
