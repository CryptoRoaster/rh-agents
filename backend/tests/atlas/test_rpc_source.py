"""Deterministic contract reads, proven without touching a network.

This is the only ATLAS component that talks to a chain, so its failure mapping,
slot decoding and chain verification are exercised directly.
"""

import pytest

from src.agents.atlas.models import AtlasSourceFailure, ProxyObservation
from src.agents.atlas.rpc_source import (
    ADMIN_SLOT,
    DECIMALS_SELECTOR,
    IMPLEMENTATION_SLOT,
    TOTAL_SUPPLY_SELECTOR,
    RpcTokenContractSource,
)
from src.core.clock import FixedClock
from src.markets.models import Availability
from src.runtime.models import ChainConfig, ErrorCode, Head, RuntimeFailure
from tests.atlas.conftest import ADMIN, TOKEN

IMPLEMENTATION = "0x" + "f6" * 20


def word(address: str | None) -> str:
    if address is None:
        return "0x" + "0" * 64
    return "0x" + "0" * 24 + address[2:]


class FakeClient:
    """Stands in for the managed RPC client; performs no I/O."""

    def __init__(self, **overrides: object) -> None:
        self.overrides = overrides
        self.calls: list[tuple[str, object]] = []
        self.verified = False

    async def verify_chain(self) -> int:
        if isinstance(self.overrides.get("verify"), Exception):
            raise self.overrides["verify"]
        self.verified = True
        return int(self.overrides.get("chain_id", 4663))

    async def block_number(self) -> int:
        return int(self.overrides.get("head", 1_000_100))

    async def block(self, number: int) -> Head:
        self.calls.append(("block", number))
        return Head(
            number=number,
            hash="0x" + "1" * 64,
            parent_hash="0x" + "2" * 64,
            timestamp=1,
        )

    async def code(self, address: str, block: int) -> str:
        self.calls.append(("code", (address, block)))
        value = self.overrides.get("code", "0x6080")
        if isinstance(value, Exception):
            raise value
        return str(value)

    async def call(self, address: str, selector: str, block: int) -> str:
        self.calls.append(("call", (address, selector, block)))
        value = self.overrides.get(selector)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return "0x" + "0" * 63 + "1"
        return str(value)

    async def storage_at(self, address: str, slot: str, block: int) -> str:
        self.calls.append(("storage", (address, slot, block)))
        value = self.overrides.get(slot)
        if isinstance(value, Exception):
            raise value
        return str(value) if value is not None else word(None)


def config(chain: str = "robinhood", chain_id: int = 4663) -> ChainConfig:
    from pydantic import SecretStr

    return ChainConfig(
        chain=chain,
        chain_id=chain_id,
        http_url=SecretStr("https://rpc.invalid/x"),
        ws_url=SecretStr("wss://rpc.invalid/x"),
        confirmations=12,
    )


def source(now, client: FakeClient, chain: str = "robinhood") -> RpcTokenContractSource:
    chain_id = 4663 if chain == "robinhood" else 56
    return RpcTokenContractSource(
        client=client, config=config(chain, chain_id), clock=FixedClock(now)
    )


async def test_the_snapshot_pins_a_safe_block_behind_the_head(now):
    client = FakeClient(head=1_000_100)
    snapshot = await source(now, client).chain_snapshot()
    # The runtime's confirmation lag is reused rather than a finality notion
    # invented here.
    assert snapshot.block_number == 1_000_100 - 12
    assert snapshot.chain_id == 4663
    assert snapshot.chain == "robinhood"
    assert client.verified is True


async def test_chain_verification_happens_before_anything_else(now):
    client = FakeClient(verify=RuntimeFailure(ErrorCode.CHAIN_ID_MISMATCH))
    with pytest.raises(RuntimeFailure):
        await source(now, client).chain_snapshot()
    # Nothing was read from a chain whose identity was not established.
    assert client.calls == []


async def test_complete_contract_reads_produce_available_facts(now):
    client = FakeClient(
        **{
            DECIMALS_SELECTOR: "0x" + "0" * 62 + "12",
            TOTAL_SUPPLY_SELECTOR: "0x" + "0" * 60 + "03e8",
        }
    )
    facts = await source(now, client).contract_facts(TOKEN, 1_000_000)
    assert facts.status == Availability.AVAILABLE
    assert facts.code_present is True
    assert facts.decimals == 18
    assert facts.total_supply_raw == 1000
    assert facts.observed_block == 1_000_000
    # Every read targeted the same block.
    blocks = {entry[1][-1] for entry in client.calls if entry[0] in {"code", "call", "storage"}}
    assert blocks == {1_000_000}


async def test_absent_code_is_recorded_as_a_measured_fact(now):
    facts = await source(now, FakeClient(code="0x")).contract_facts(TOKEN, 1)
    assert facts.status == Availability.AVAILABLE
    assert facts.code_present is False


@pytest.mark.parametrize(
    "error,expected",
    [
        (ErrorCode.TIMEOUT, AtlasSourceFailure.TIMEOUT),
        (ErrorCode.CONFIGURATION, AtlasSourceFailure.NOT_CONFIGURED),
        (ErrorCode.CHAIN_ID_MISMATCH, AtlasSourceFailure.CHAIN_MISMATCH),
        (ErrorCode.CONTRACT, AtlasSourceFailure.INVALID_RESPONSE),
        (ErrorCode.UNAVAILABLE, AtlasSourceFailure.UNAVAILABLE),
    ],
)
async def test_a_failed_code_read_maps_to_a_typed_failure(now, error, expected):
    facts = await source(now, FakeClient(code=RuntimeFailure(error))).contract_facts(TOKEN, 1)
    assert facts.status == Availability.UNAVAILABLE
    assert facts.failure == expected
    # An unavailable domain carries no observations at all.
    assert facts.code_present is None


async def test_a_reverting_standard_call_leaves_the_field_unknown(now):
    """A heterogeneous ERC-20 may not implement decimals; that is not a default."""
    client = FakeClient(**{DECIMALS_SELECTOR: RuntimeFailure(ErrorCode.CONTRACT)})
    facts = await source(now, client).contract_facts(TOKEN, 1)
    assert facts.status == Availability.AVAILABLE
    assert facts.decimals is None
    assert facts.total_supply is None


async def test_an_empty_call_result_is_unknown_not_zero(now):
    client = FakeClient(**{TOTAL_SUPPLY_SELECTOR: "0x"})
    facts = await source(now, client).contract_facts(TOKEN, 1)
    assert facts.total_supply_raw is None


async def test_an_implausible_decimals_value_is_refused(now):
    client = FakeClient(**{DECIMALS_SELECTOR: "0x" + "f" * 64})
    facts = await source(now, client).contract_facts(TOKEN, 1)
    assert facts.decimals is None


async def test_eip1967_slots_are_decoded_to_addresses(now):
    client = FakeClient(**{IMPLEMENTATION_SLOT: word(IMPLEMENTATION), ADMIN_SLOT: word(ADMIN)})
    facts = await source(now, client).contract_facts(TOKEN, 1)
    assert facts.proxy == ProxyObservation.EIP1967_DETECTED
    assert facts.implementation_address == IMPLEMENTATION
    assert facts.admin_address == ADMIN


async def test_empty_slots_are_not_a_proof_of_absence(now):
    facts = await source(now, FakeClient()).contract_facts(TOKEN, 1)
    # These documented slots were read and were empty. That is a weaker claim
    # than "this contract is not a proxy".
    assert facts.proxy == ProxyObservation.EIP1967_SLOTS_EMPTY
    assert facts.implementation_address is None


async def test_unreadable_slots_are_not_checked_rather_than_empty(now):
    client = FakeClient(**{IMPLEMENTATION_SLOT: RuntimeFailure(ErrorCode.UNAVAILABLE)})
    facts = await source(now, client).contract_facts(TOKEN, 1)
    assert facts.proxy == ProxyObservation.NOT_CHECKED


def test_the_documented_eip1967_slots_are_used_verbatim():
    assert IMPLEMENTATION_SLOT == (
        "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
    )
    assert ADMIN_SLOT == "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"
    assert DECIMALS_SELECTOR == "0x313ce567"
    assert TOTAL_SUPPLY_SELECTOR == "0x18160ddd"
