import asyncio

from src.core.models import AgentDecision


class DecisionBus:
    """Bounded in-process typed transport, not a durable source of truth."""

    def __init__(self, capacity: int = 100) -> None:
        self._queue: asyncio.Queue[AgentDecision] = asyncio.Queue(maxsize=capacity)

    async def publish(self, decision: AgentDecision) -> None:
        validated = AgentDecision.model_validate_json(decision.model_dump_json())
        await self._queue.put(validated)

    async def receive(self) -> AgentDecision:
        return await self._queue.get()

    def acknowledge(self) -> None:
        self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()
