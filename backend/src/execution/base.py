from abc import ABC, abstractmethod

from src.core.models import ExecutionResult, MarketSnapshot, OrderIntent


class Executor(ABC):
    """Infrastructure boundary; only a future implementation may access a signer."""

    @abstractmethod
    async def execute(self, order: OrderIntent, market: MarketSnapshot) -> ExecutionResult:
        """Execute an approved order; implementations must preserve its trace."""
        raise NotImplementedError
