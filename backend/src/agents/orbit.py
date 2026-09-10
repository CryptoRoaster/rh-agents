"""Future ORBIT input capability: recorded data only, no DB writes or transport clients.

Trusted infrastructure implements this port. Agent workers must receive serialized
records via a read-only service boundary, never the recorder or session factory.
"""

from typing import Protocol

from src.markets.models import MarketCandidate, MarketSnapshot


class OrbitMarketInput(Protocol):
    async def latest(
        self, identity: str, *, include_fixtures: bool = False
    ) -> MarketSnapshot | None: ...

    async def candidates(
        self,
        *,
        include_fixtures: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[MarketCandidate, ...]: ...
