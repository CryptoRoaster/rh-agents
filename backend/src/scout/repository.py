"""Durable scout state: watches, their schedule, and their assessment history.

Every schedule transition is a compare-and-set on the field it advances. Two
scout runs that race for the same due watch cannot both take the checkpoint:
the one whose update matched the expected value owns it, and the other moves on
without having called anybody. The unique `(watch_id, checkpoint_index)`
constraint then holds the same line in the database itself.

Nothing here calls a provider or a model, and no provider call is ever made
while a transaction opened here is still open.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.data.repository import aware
from src.data.tables import (
    DiscoveryWatchAssessmentRow,
    DiscoveryWatchRow,
    MarketObservationRow,
)
from src.markets.models import MarketIdentity, MarketSnapshot
from src.scout.models import DiscoveryWatch, WatchAssessment
from src.scout.policy import EARLY_SCOUT_V1, REVIEWABLE, EarlyScoutPolicy, WatchStatus

WATCH_SCHEMA_VERSION = 1


class SyncResult(StrEnum):
    """What recording one observation did to the watch of its market."""

    CREATED = "CREATED"
    UPDATED = "UPDATED"
    # The observation is not newer than what the watch already holds.
    UNCHANGED = "UNCHANGED"
    # No watch existed and this run's creation budget did not allow one.
    SKIPPED = "SKIPPED"
    # The observation names the same stream with a different market identity.
    CONFLICT = "CONFLICT"


def _watch(row: DiscoveryWatchRow) -> DiscoveryWatch:
    def maybe(value: datetime | None) -> datetime | None:
        return None if value is None else aware(value)

    return DiscoveryWatch(
        id=row.id,
        schema_version=row.schema_version,
        policy_version=row.policy_version,
        provider=row.provider,
        chain=row.chain,
        network=row.network,
        pair_id=row.pair_id,
        is_fixture=row.is_fixture,
        market=MarketIdentity.model_validate(row.market_payload),
        first_seen_at=aware(row.first_seen_at),
        last_seen_at=aware(row.last_seen_at),
        latest_snapshot_id=row.latest_snapshot_id,
        created_at=aware(row.created_at),
        updated_at=aware(row.updated_at),
        status=WatchStatus(row.status),
        next_orbit_review_at=maybe(row.next_orbit_review_at),
        orbit_checkpoint_index=row.orbit_checkpoint_index,
        next_history_review_at=maybe(row.next_history_review_at),
        latest_vector_sufficiency=row.latest_vector_sufficiency,
        vector_checked_at=maybe(row.vector_checked_at),
        reason_code=row.reason_code,
        last_promoted_trade_case_id=row.last_promoted_trade_case_id,
    )


def _assessment(row: DiscoveryWatchAssessmentRow) -> WatchAssessment:
    return WatchAssessment(
        id=row.id,
        watch_id=row.watch_id,
        snapshot_id=row.snapshot_id,
        assessed_at=aware(row.assessed_at),
        checkpoint_index=row.checkpoint_index,
        checkpoint_seconds=row.checkpoint_seconds,
        status=row.status,  # type: ignore[arg-type]
        failure_reason=row.failure_reason,
        classification=row.classification,
        strength=row.strength,
        reason_codes=tuple(row.reason_codes),
        data_gaps=tuple(row.data_gaps),
        cited_observation_ids=tuple(UUID(item) for item in row.cited_observation_ids),
        summary=row.summary,
        input_digest=row.input_digest,
        policy_version=row.policy_version,
        prompt_version=row.prompt_version,
        prompt_hash=row.prompt_hash,
        output_schema_version=row.output_schema_version,
        reasoning_provider=row.reasoning_provider,
        reasoning_model=row.reasoning_model,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        latency_ms=row.latency_ms,
    )


def _same_market(stored: MarketIdentity, observed: MarketIdentity) -> bool:
    """Whether an observation describes the market a watch was opened for.

    Equality, with one exception: a watch adopted from a stream recorded before
    pool locators existed has none, and a later observation that adds one for
    the otherwise identical market completes the identity rather than
    contradicting it. Nothing else is reconciled.
    """
    if stored == observed:
        return True
    return stored.pool_locator is None and stored == observed.model_copy(
        update={"pool_locator": None}
    )


@dataclass(frozen=True)
class WatchRepository:
    sessions: async_sessionmaker[AsyncSession]
    policy: EarlyScoutPolicy = EARLY_SCOUT_V1

    # ------------------------------------------------------------------ reads

    async def get(self, watch_id: UUID) -> DiscoveryWatch | None:
        async with self.sessions() as session:
            row = await session.get(DiscoveryWatchRow, watch_id)
            return None if row is None else _watch(row)

    async def by_pair(self, pair_id: str, *, is_fixture: bool = False) -> DiscoveryWatch | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(DiscoveryWatchRow)
                .where(
                    DiscoveryWatchRow.pair_id == pair_id,
                    DiscoveryWatchRow.is_fixture.is_(is_fixture),
                )
                .order_by(DiscoveryWatchRow.provider)
                .limit(1)
            )
            return None if row is None else _watch(row)

    async def assessments(self, watch_id: UUID) -> tuple[WatchAssessment, ...]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(DiscoveryWatchAssessmentRow)
                    .where(DiscoveryWatchAssessmentRow.watch_id == watch_id)
                    .order_by(
                        DiscoveryWatchAssessmentRow.assessed_at,
                        DiscoveryWatchAssessmentRow.checkpoint_index,
                    )
                )
            ).all()
            return tuple(_assessment(row) for row in rows)

    async def snapshot(self, snapshot_id: UUID) -> MarketSnapshot | None:
        """The exact recorded observation a watch points at."""
        async with self.sessions() as session:
            row = await session.get(MarketObservationRow, snapshot_id)
            return None if row is None else MarketSnapshot.model_validate(row.payload)

    def _due(self, column: Any, statuses: frozenset[WatchStatus], now: datetime) -> Any:
        return and_(
            DiscoveryWatchRow.status.in_([item.value for item in statuses]),
            column.is_not(None),
            column <= now,
        )

    async def due_for_orbit(self, now: datetime, limit: int) -> tuple[DiscoveryWatch, ...]:
        """Due reviews in schedule order, then discovery order, then identity.

        Never by liquidity, volume, price or any earlier classification: the
        order a budget cuts at must not become a hidden strategy.
        """
        return await self._select_due(
            DiscoveryWatchRow.next_orbit_review_at, REVIEWABLE, now, limit
        )

    async def due_for_history(self, now: datetime, limit: int) -> tuple[DiscoveryWatch, ...]:
        return await self._select_due(
            DiscoveryWatchRow.next_history_review_at,
            frozenset({WatchStatus.WATCHING}),
            now,
            limit,
        )

    async def _select_due(
        self, column: Any, statuses: frozenset[WatchStatus], now: datetime, limit: int
    ) -> tuple[DiscoveryWatch, ...]:
        if limit <= 0:
            return ()
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(DiscoveryWatchRow)
                    .where(self._due(column, statuses, now))
                    .order_by(column, DiscoveryWatchRow.first_seen_at, DiscoveryWatchRow.pair_id)
                    .limit(limit)
                )
            ).all()
            return tuple(_watch(row) for row in rows)

    async def count_due(self, now: datetime) -> tuple[int, int]:
        """How many watches are due for ORBIT and for history, before any budget."""
        async with self.sessions() as session:
            orbit = await session.scalar(
                select(func.count())
                .select_from(DiscoveryWatchRow)
                .where(self._due(DiscoveryWatchRow.next_orbit_review_at, REVIEWABLE, now))
            )
            history = await session.scalar(
                select(func.count())
                .select_from(DiscoveryWatchRow)
                .where(
                    self._due(
                        DiscoveryWatchRow.next_history_review_at,
                        frozenset({WatchStatus.WATCHING}),
                        now,
                    )
                )
            )
        return int(orbit or 0), int(history or 0)

    async def promotable(self, limit: int) -> tuple[DiscoveryWatch, ...]:
        """PROMOTABLE watches in discovery order. Never ranked by market size."""
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(DiscoveryWatchRow)
                    .where(DiscoveryWatchRow.status == WatchStatus.PROMOTABLE.value)
                    .order_by(DiscoveryWatchRow.first_seen_at, DiscoveryWatchRow.pair_id)
                    .limit(limit)
                )
            ).all()
            return tuple(_watch(row) for row in rows)

    # ----------------------------------------------------------------- writes

    async def sync(
        self, snapshot: MarketSnapshot, *, now: datetime, allow_create: bool
    ) -> SyncResult:
        """Attach one recorded observation to the watch of its market.

        `first_seen_at` is written once, from the observation's own source
        instant, and never moved. `last_seen_at` and the latest snapshot only
        ever move forward.
        """
        identity = snapshot.pair.market_identity
        key = self._stream(identity)
        async with self.sessions.begin() as session:
            row = await session.scalar(select(DiscoveryWatchRow).where(*key).with_for_update())
            if row is None:
                if not allow_create:
                    return SyncResult.SKIPPED
                if await self._insert(session, snapshot, now, "WATCH_OPENED"):
                    return SyncResult.CREATED
                # Somebody else created it between the read and the insert.
                row = await session.scalar(select(DiscoveryWatchRow).where(*key).with_for_update())
                if row is None:  # pragma: no cover - the conflict implies the row
                    return SyncResult.SKIPPED
            stored = MarketIdentity.model_validate(row.market_payload)
            if not _same_market(stored, identity):
                return SyncResult.CONFLICT
            if snapshot.observed_at <= aware(row.last_seen_at):
                return SyncResult.UNCHANGED
            row.market_payload = identity.model_dump(mode="json")
            row.last_seen_at = snapshot.observed_at
            row.latest_snapshot_id = snapshot.id
            row.updated_at = now
            return SyncResult.UPDATED

    @staticmethod
    def _stream(identity: MarketIdentity) -> tuple[Any, ...]:
        return (
            DiscoveryWatchRow.provider == identity.provider,
            DiscoveryWatchRow.chain == identity.chain,
            DiscoveryWatchRow.network == identity.network,
            DiscoveryWatchRow.pair_id == identity.pair_id,
            DiscoveryWatchRow.is_fixture.is_(identity.is_fixture),
        )

    async def _insert(
        self,
        session: AsyncSession,
        snapshot: MarketSnapshot,
        now: datetime,
        reason: str,
        *,
        first_seen_at: datetime | None = None,
    ) -> bool:
        identity = snapshot.pair.market_identity
        first = first_seen_at if first_seen_at is not None else snapshot.observed_at
        values = dict(
            id=uuid4(),
            schema_version=WATCH_SCHEMA_VERSION,
            policy_version=self.policy.version,
            provider=identity.provider,
            chain=identity.chain,
            network=identity.network,
            pair_id=identity.pair_id,
            is_fixture=identity.is_fixture,
            market_payload=identity.model_dump(mode="json"),
            first_seen_at=first,
            last_seen_at=snapshot.observed_at,
            latest_snapshot_id=snapshot.id,
            created_at=now,
            updated_at=now,
            status=WatchStatus.WATCHING.value,
            next_orbit_review_at=self.policy.first_orbit_review_at(first),
            orbit_checkpoint_index=None,
            next_history_review_at=self.policy.first_history_review_at(first),
            latest_vector_sufficiency=None,
            vector_checked_at=None,
            reason_code=reason,
            last_promoted_trade_case_id=None,
        )
        dialect = session.get_bind().dialect.name
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        statement = (
            insert(DiscoveryWatchRow)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["provider", "chain", "network", "pair_id", "is_fixture"]
            )
            .returning(DiscoveryWatchRow.id)
        )
        return await session.scalar(statement) is not None

    async def claim_orbit(
        self,
        watch_id: UUID,
        *,
        expected: datetime,
        next_at: datetime | None,
        checkpoint_index: int,
        now: datetime,
    ) -> bool:
        """Take one due checkpoint, before anybody is asked anything.

        The checkpoint is spent whether or not the review then succeeds. A
        failed review is recorded and the watch waits for its next checkpoint:
        a provider outage must never turn into a loop of paid retries.
        """
        async with self.sessions.begin() as session:
            result = await session.execute(
                update(DiscoveryWatchRow)
                .where(
                    DiscoveryWatchRow.id == watch_id,
                    DiscoveryWatchRow.next_orbit_review_at == expected,
                    DiscoveryWatchRow.status.in_([item.value for item in REVIEWABLE]),
                )
                .values(
                    next_orbit_review_at=next_at,
                    orbit_checkpoint_index=checkpoint_index,
                    updated_at=now,
                )
            )
            return bool(getattr(result, "rowcount", 0) == 1)

    async def append_assessment(self, assessment: WatchAssessment) -> None:
        async with self.sessions.begin() as session:
            session.add(
                DiscoveryWatchAssessmentRow(
                    **assessment.model_dump(
                        exclude={"reason_codes", "data_gaps", "cited_observation_ids"}
                    ),
                    reason_codes=list(assessment.reason_codes),
                    data_gaps=list(assessment.data_gaps),
                    cited_observation_ids=[str(item) for item in assessment.cited_observation_ids],
                )
            )

    async def settle_history(
        self,
        watch_id: UUID,
        *,
        expected_next: datetime | None,
        verdict: str,
        status: WatchStatus,
        next_at: datetime | None,
        now: datetime,
    ) -> bool:
        """Record one history verdict and the lifecycle step it implies."""
        values: dict[str, Any] = dict(
            status=status.value,
            next_history_review_at=next_at,
            latest_vector_sufficiency=verdict,
            vector_checked_at=now,
            reason_code=f"HISTORY_{verdict}"[:80]
            if status is WatchStatus.WATCHING
            else status.value,
            updated_at=now,
        )
        if status is WatchStatus.DORMANT:
            # Dormant causes no further traffic of any kind.
            values["next_orbit_review_at"] = None
        async with self.sessions.begin() as session:
            result = await session.execute(
                update(DiscoveryWatchRow)
                .where(
                    DiscoveryWatchRow.id == watch_id,
                    DiscoveryWatchRow.status == WatchStatus.WATCHING.value,
                    DiscoveryWatchRow.next_history_review_at == expected_next,
                )
                .values(**values)
            )
            return bool(getattr(result, "rowcount", 0) == 1)

    async def note(self, watch_id: UUID, reason: str, now: datetime) -> None:
        """Record why a due step did not happen. Changes no schedule."""
        async with self.sessions.begin() as session:
            await session.execute(
                update(DiscoveryWatchRow)
                .where(DiscoveryWatchRow.id == watch_id)
                .values(reason_code=reason, updated_at=now)
            )

    async def retire(self, watch_id: UUID, reason: str, now: datetime) -> None:
        """Fail closed on a market that can no longer be addressed as itself."""
        async with self.sessions.begin() as session:
            await session.execute(
                update(DiscoveryWatchRow)
                .where(DiscoveryWatchRow.id == watch_id)
                .values(
                    status=WatchStatus.RETIRED.value,
                    next_orbit_review_at=None,
                    next_history_review_at=None,
                    reason_code=reason,
                    updated_at=now,
                )
            )

    async def mark_promoted(self, pair_id: str, trade_case_id: UUID, now: datetime) -> None:
        """Remember which case a PROMOTABLE watch last formed. Audit only."""
        async with self.sessions.begin() as session:
            await session.execute(
                update(DiscoveryWatchRow)
                .where(
                    DiscoveryWatchRow.pair_id == pair_id,
                    DiscoveryWatchRow.status == WatchStatus.PROMOTABLE.value,
                )
                .values(last_promoted_trade_case_id=trade_case_id, updated_at=now)
            )

    async def bootstrap(self, now: datetime, limit: int) -> int:
        """Adopt recorded market streams that have no watch yet. Bounded, idempotent.

        Oldest stream first. `first_seen_at` is the stream's own oldest
        observation, and the watch points at its newest one. No model and no
        provider is involved: an adopted watch is reviewed later under the same
        budgets as any other.
        """
        if limit <= 0:
            return 0
        row = MarketObservationRow
        first = func.min(row.observed_at).label("first_seen")
        watched = exists(
            select(DiscoveryWatchRow.id).where(
                DiscoveryWatchRow.provider == row.provider,
                DiscoveryWatchRow.chain == row.chain,
                DiscoveryWatchRow.network == row.network,
                DiscoveryWatchRow.pair_id == row.pair_id,
                DiscoveryWatchRow.is_fixture.is_(False),
            )
        )
        async with self.sessions() as session:
            streams = (
                await session.execute(
                    select(row.provider, row.chain, row.network, row.pair_id, first)
                    .where(row.is_fixture.is_(False), ~watched)
                    .group_by(row.provider, row.chain, row.network, row.pair_id)
                    .order_by(first, row.pair_id)
                    .limit(limit)
                )
            ).all()
        adopted = 0
        for provider, chain, network, pair_id, first_seen in streams:
            async with self.sessions() as session:
                latest = await session.scalar(
                    select(row)
                    .where(
                        row.provider == provider,
                        row.chain == chain,
                        row.network == network,
                        row.pair_id == pair_id,
                        row.is_fixture.is_(False),
                    )
                    .order_by(row.observed_at.desc(), row.recorded_at.desc(), row.id.desc())
                    .limit(1)
                )
            if latest is None:  # pragma: no cover - the stream was just grouped
                continue
            snapshot = MarketSnapshot.model_validate(latest.payload)
            async with self.sessions.begin() as session:
                if await self._insert(
                    session, snapshot, now, "WATCH_BOOTSTRAPPED", first_seen_at=aware(first_seen)
                ):
                    adopted += 1
        return adopted
