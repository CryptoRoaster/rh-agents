"""The V4 census read port over the managed, read-only EVM RPC client."""

from dataclasses import dataclass

from src.agents.atlas.v4.census import ChainLog
from src.runtime.rpc import EvmRpcClient


@dataclass(frozen=True)
class RpcV4ChainReads:
    client: EvmRpcClient

    async def chain_id(self) -> int:
        return await self.client.verify_chain()

    async def logs(
        self, address: str, topics: tuple[str | None, ...], start: int, end: int
    ) -> tuple[ChainLog, ...]:
        found = await self.client.event_logs(address, topics, start, end)
        return tuple(
            ChainLog(block_number=item.block_number, topics=item.topics, data=item.data)
            for item in found
        )

    async def block_timestamp(self, number: int) -> int:
        return (await self.client.block(number)).timestamp

    async def code(self, address: str, block: int) -> str:
        return await self.client.code(address, block)

    async def call(self, address: str, selector: str, block: int) -> str:
        return await self.client.call(address, selector, block)

    async def call_word(self, address: str, selector: str, argument: str, block: int) -> str:
        return await self.client.call_word(address, selector, argument, block)

    async def balance_of(self, token: str, holder: str, block: int) -> int:
        return await self.client.balance_of(token, holder, block)

    async def storage(self, address: str, slot: str, block: int) -> str:
        return await self.client.storage_at(address, slot, block)
