"""Validated provider network bindings, kept for a bounded time across scout runs.

GeckoTerminal lists Robinhood on page 3 of its paginated `/networks` directory,
so validating the binding costs a scout run three of its five requests and
leaves no room for page 2 of `new_pools`. A binding `NetworkDirectory` has
validated is therefore stored, and a later run may answer from it — but only
for exactly what was validated: the same provider, chain, configured network
id and platform, verified no later than now and less than the TTL ago. Every
other stored row is refused and the chain is validated again, the usual way.

A failure is never stored: a row exists only because a validation succeeded.
A binding the provider then refuses to answer for is deleted, so the next run
validates again. Nothing here makes a request.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.config import Settings
from src.data.repository import aware
from src.data.tables import ProviderNetworkBindingRow
from src.markets.geckoterminal.networks import CHAINS, Chain, VerifiedNetworkRegistry

PROVIDER = "geckoterminal"


def configured_id(settings: Settings, chain: Chain) -> str:
    return {
        "bsc": settings.geckoterminal_bsc_network_id,
        "robinhood": settings.geckoterminal_robinhood_network_id,
    }[chain.name]


@dataclass(frozen=True)
class Loaded:
    registry: VerifiedNetworkRegistry
    # Stored rows for these chains that were not exactly a valid binding.
    rejected: int


@dataclass(frozen=True)
class NetworkBindingCache:
    sessions: async_sessionmaker[AsyncSession]
    settings: Settings

    @property
    def ttl(self) -> timedelta:
        return timedelta(seconds=self.settings.early_scout_network_cache_ttl_seconds)

    async def load(self, chains: tuple[Chain, ...], now: datetime) -> Loaded:
        """A registry seeded with every stored binding still valid at `now`."""
        registry = VerifiedNetworkRegistry()
        if self.ttl <= timedelta(0):
            return Loaded(registry, 0)
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(ProviderNetworkBindingRow).where(
                        ProviderNetworkBindingRow.provider == PROVIDER,
                        ProviderNetworkBindingRow.chain.in_([chain.name for chain in chains]),
                    )
                )
            ).all()
        rejected = 0
        for row in rows:
            chain = CHAINS[row.chain]
            age = now - aware(row.verified_at)
            if (
                row.network_id == configured_id(self.settings, chain)
                and row.platform == chain.platform
                and timedelta(0) <= age < self.ttl
            ):
                registry.seed(chain.name, row.network_id)
            else:
                rejected += 1
        return Loaded(registry, rejected)

    async def store(self, registry: VerifiedNetworkRegistry, now: datetime) -> None:
        """Keep what this run validated against the provider, and only that."""
        validated = registry.validated()
        if self.ttl <= timedelta(0) or not validated:
            return
        async with self.sessions.begin() as session:
            dialect = session.get_bind().dialect.name
            insert = pg_insert if dialect == "postgresql" else sqlite_insert
            for name, network_id in validated.items():
                values = dict(
                    provider=PROVIDER,
                    chain=name,
                    network_id=network_id,
                    platform=CHAINS[name].platform,
                    verified_at=now,
                )
                await session.execute(
                    insert(ProviderNetworkBindingRow)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["provider", "chain"],
                        set_={
                            "network_id": network_id,
                            "platform": values["platform"],
                            "verified_at": now,
                        },
                    )
                )

    async def forget(self, chain: Chain, registry: VerifiedNetworkRegistry) -> None:
        """Drop a binding the provider no longer answers for, here and stored."""
        registry.drop(chain.name)
        async with self.sessions.begin() as session:
            await session.execute(
                delete(ProviderNetworkBindingRow).where(
                    ProviderNetworkBindingRow.provider == PROVIDER,
                    ProviderNetworkBindingRow.chain == chain.name,
                )
            )
