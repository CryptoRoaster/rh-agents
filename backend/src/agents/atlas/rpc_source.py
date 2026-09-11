"""Deterministic token contract facts over the managed EVM RPC client.

Infrastructure, never handed to a worker. Every value here comes from an
explicitly supported standard: ERC-20 ``decimals()`` and ``totalSupply()``, and
the exact documented EIP-1967 proxy slots. Non-standard methods such as
``owner()`` are deliberately absent, because a call that reverts or succeeds on
a heterogeneous ERC-20 proves nothing about intent.

All reads for one snapshot target the same block, so facts used together are
consistent rather than assembled across a moving chain.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from src.agents.atlas.models import (
    AtlasSourceFailure,
    ChainSnapshot,
    ContractFacts,
    ProxyObservation,
)
from src.core.clock import Clock, SystemClock
from src.markets.models import Availability
from src.runtime.models import ChainConfig, ErrorCode, RuntimeFailure
from src.runtime.rpc import EvmRpcClient

# ERC-20 standard selectors.
DECIMALS_SELECTOR = "0x313ce567"
TOTAL_SUPPLY_SELECTOR = "0x18160ddd"

# EIP-1967 storage slots, exactly as documented:
# keccak256("eip1967.proxy.implementation") - 1 and keccak256("eip1967.proxy.admin") - 1.
IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
ADMIN_SLOT = "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"

FAILURES: dict[ErrorCode, AtlasSourceFailure] = {
    ErrorCode.TIMEOUT: AtlasSourceFailure.TIMEOUT,
    ErrorCode.CONFIGURATION: AtlasSourceFailure.NOT_CONFIGURED,
    ErrorCode.CHAIN_ID_MISMATCH: AtlasSourceFailure.CHAIN_MISMATCH,
    ErrorCode.CONTRACT: AtlasSourceFailure.INVALID_RESPONSE,
    ErrorCode.UNAVAILABLE: AtlasSourceFailure.UNAVAILABLE,
}


def _address_from_slot(word: str) -> str | None:
    """The low 20 bytes of a storage word, or None when the slot is empty."""
    value = int(word, 16)
    if value == 0:
        return None
    return "0x" + word[-40:]


@dataclass(frozen=True)
class RpcTokenContractSource:
    client: EvmRpcClient
    config: ChainConfig
    clock: Clock = SystemClock()
    source: str = "evm-rpc"

    async def chain_snapshot(self) -> ChainSnapshot:
        """Verify the chain, then pin one safe block for the whole snapshot."""
        chain_id = await self.client.verify_chain()
        head = await self.client.block_number()
        # Reuse the runtime's confirmation lag rather than inventing a finality
        # notion here. No claim of finality is made beyond that lag.
        safe = max(0, head - self.config.confirmations)
        block = await self.client.block(safe)
        return ChainSnapshot(
            chain=self.config.chain,
            network=self.config.network,
            chain_id=chain_id,
            block_number=block.number,
            block_hash=block.hash if block.hash.startswith("0x") else None,
            # Chain time, which is what "how current is this" actually means. A
            # block mined twenty minutes ago is twenty minutes old however
            # recently we asked for it.
            block_timestamp=datetime.fromtimestamp(block.timestamp, UTC),
            observed_at=self.clock.now().astimezone(UTC),
            source=self.source,
        )

    async def contract_facts(self, token_address: str, block: int) -> ContractFacts:
        try:
            code = await self.client.code(token_address, block)
        except RuntimeFailure as error:
            return ContractFacts(
                status=Availability.UNAVAILABLE,
                failure=FAILURES.get(error.code, AtlasSourceFailure.UNAVAILABLE),
                source=self.source,
            )
        code_present = len(code) > 2
        decimals = await self._call_int(token_address, DECIMALS_SELECTOR, block)
        supply = await self._call_int(token_address, TOTAL_SUPPLY_SELECTOR, block)
        proxy, implementation, admin = await self._proxy(token_address, block)
        return ContractFacts(
            status=Availability.AVAILABLE,
            source=self.source,
            observed_block=block,
            code_present=code_present,
            code_hash=None,
            # A reverting or absent standard call leaves the field unknown rather
            # than defaulting it to something convenient.
            decimals=decimals if decimals is not None and 0 <= decimals <= 36 else None,
            total_supply_raw=supply,
            proxy=proxy,
            implementation_address=implementation,
            admin_address=admin,
        )

    async def creation_receipt_contract(self, tx_hash: str) -> str | None:
        """Chain-side confirmation of a creation claim, or None when unobtainable.

        A provider can say anything about who deployed a token. The creation
        receipt is the chain's own answer, so it is what a claim is checked
        against. A failed read returns None, which leaves the claim unverified
        rather than silently confirming it.
        """
        try:
            return await self.client.receipt_contract_address(tx_hash)
        except RuntimeFailure:
            return None

    async def is_contract(self, address: str, block: int) -> bool | None:
        """Whether a creator address is itself code, at the pinned block.

        Many tokens are deployed by factories. Knowing that the creator is a
        contract keeps it from being read as a person's wallet; it is recorded as
        a fact and interpreted no further here.
        """
        try:
            code = await self.client.code(address, block)
        except RuntimeFailure:
            return None
        return len(code) > 2

    async def _call_int(self, address: str, selector: str, block: int) -> int | None:
        try:
            result = await self.client.call(address, selector, block)
        except RuntimeFailure:
            return None
        if len(result) <= 2:
            return None
        try:
            return int(result, 16)
        except ValueError:
            return None

    async def _proxy(
        self, address: str, block: int
    ) -> tuple[ProxyObservation, str | None, str | None]:
        try:
            implementation_word = await self.client.storage_at(address, IMPLEMENTATION_SLOT, block)
            admin_word = await self.client.storage_at(address, ADMIN_SLOT, block)
        except RuntimeFailure:
            return ProxyObservation.NOT_CHECKED, None, None
        implementation = _address_from_slot(implementation_word)
        admin = _address_from_slot(admin_word)
        if implementation is None and admin is None:
            # Empty EIP-1967 slots mean this is not an EIP-1967 proxy. It does not
            # prove the contract is not a proxy of some other pattern.
            return ProxyObservation.EIP1967_SLOTS_EMPTY, None, None
        return ProxyObservation.EIP1967_DETECTED, implementation, admin
