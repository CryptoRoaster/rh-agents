"""Durable scout state: watches, their schedule, and their assessment history.

Every schedule transition is a compare-and-set on the field it advances. Two
scout runs that race for the same due watch cannot both take the checkpoint:
the one whose update matched the expected value owns it, and the other moves on
without having called anybody. The unique `(watch_id, checkpoint_index)`
constraint then holds the same line in the database itself.

Nothing here calls a provider or a model, and no provider call is ever made
while a transaction opened here is still open.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.data.repository import aware
from src.data.tables import (
    DiscoveryStreamDeclineRow,
    DiscoveryWatchAssessmentRow,
    DiscoveryWatchRow,
    MarketObservationRow,
)
from src.markets.models import MarketIdentity, MarketSnapshot
from src.markets.scope import describes_market
from src.scout.models import DiscoveryWatch, WatchAssessment
from src.scout.policy import (
    EARLY_SCOUT_V2,
    REVIEWABLE,
    EarlyScoutPolicy,
    OrbitState,
    WatchStatus,
)

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
        failure_reason_code=row.failure_reason_code,
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
        reasoning_effort=row.reasoning_effort,
        reported_effort=row.reported_effort,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        latency_ms=row.latency_ms,
    )


def refreshed_identity_contradicts(stored: MarketIdentity, observed: MarketIdentity) -> bool:
    """Whether a pool re-observed by its stored locator now names another market.

    The one identity check for exact-locator refresh, used by the scout and by
    the full run's promotion refresh alike. The locator is compared apart from
    the rest: it is what was asked for, so the question is whether everything
    else the answer says about the market still agrees with the watch.
    """
    return stored.model_copy(update={"pool_locator": None}) != observed.model_copy(
        update={"pool_locator": None}
    )


def _same_market(stored: MarketIdentity, observed: MarketIdentity) -> bool:
    """Whether an observation describes the market a watch was opened for.

    Equality, with one exception: a watch adopted from a stream recorded before
    pool locators existed has none, and a later observation that adds one for
    the otherwise identical market completes the identity rather than
    contradicting it. Nothing else is reconciled.
    """
    return describes_market(observed, stored)


@dataclass(frozen=True)
class Backlog:
    """ORBIT work that is due and not yet done, at one instant."""

    due: int
    oldest_due_age_seconds: int | None
    # Reviewable watches that have never had an ORBIT review and still may.
    unreviewed: int
    # What became of review debt that will not be served (EARLY_SCOUT_V2).
    skipped_stale: int = 0
    follow_ups_deferred: int = 0
    reviewed: int = 0


@dataclass(frozen=True)
class OrbitSettlement:
    """Review debt one run closed without a model call."""

    skipped_stale: int = 0
    follow_ups_deferred: int = 0


# The upper bound on fresh first reviews read to choose among. A freshness
# window of an hour holds a few dozen at the measured intake; this only keeps a
# pathological burst from becoming an unbounded read.
FRESH_SCAN = 2000


def selection_key(policy_version: str, watch: DiscoveryWatch) -> str:
    """The signal-blind order fresh first reviews are chosen in.

    A hash of the policy version and the stable market identity and nothing
    else: no price, liquidity, volume, momentum, JEV answer or model opinion
    can move a watch up or down. The same watch always sorts the same way under
    the same policy, on both chains alike.
    """
    identity = "|".join(
        (
            policy_version,
            watch.provider,
            watch.chain,
            watch.network,
            watch.pair_id,
            str(watch.is_fixture),
        )
    )
    return sha256(identity.encode()).hexdigest()


@dataclass(frozen=True)
class WatchRepository:
    sessions: async_sessionmaker[AsyncSession]
    policy: EarlyScoutPolicy = EARLY_SCOUT_V2

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
        """Due reviews, in an order no market figure or opinion can influence.

        V1: schedule order, then discovery order, then identity. V2 (fresh first
        only): first reviews still inside the freshness window, ordered by the
        signal-blind `selection_key` — never by liquidity, volume, price, JEV or
        any earlier classification: the order a budget cuts at must not become
        a hidden strategy.
        """
        if not self.policy.fresh_first_only:
            return await self._select_due(
                DiscoveryWatchRow.next_orbit_review_at, REVIEWABLE, now, limit
            )
        if limit <= 0:
            return ()
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(DiscoveryWatchRow)
                    .where(self._fresh_first(now))
                    .order_by(DiscoveryWatchRow.first_seen_at, DiscoveryWatchRow.pair_id)
                    .limit(FRESH_SCAN)
                )
            ).all()
        fresh = [_watch(row) for row in rows]
        fresh.sort(key=lambda watch: selection_key(self.policy.version, watch))
        return tuple(fresh[:limit])

    def _fresh_first(self, now: datetime) -> Any:
        """A pending first review, due, and still inside the freshness window."""
        window = self.policy.first_review_window
        assert window is not None  # only called under a fresh-first policy
        return and_(
            self._due(DiscoveryWatchRow.next_orbit_review_at, REVIEWABLE, now),
            DiscoveryWatchRow.orbit_checkpoint_index.is_(None),
            DiscoveryWatchRow.orbit_state.is_(None),
            DiscoveryWatchRow.first_seen_at >= now - window,
        )

    async def settle_orbit_debts(self, now: datetime) -> OrbitSettlement:
        """Close review debt V2 will not serve, deterministically and without a call.

        Two bulk updates, each idempotent because it only touches rows that
        still owe a review: a first review not taken within the freshness
        window is `ORBIT_FIRST_REVIEW_SKIPPED_STALE`; a watch already reviewed
        whose V1 follow-ups are still scheduled is `ORBIT_FOLLOW_UPS_DEFERRED`.
        Nothing else changes — status, history schedule, assessments and the
        watch itself stay exactly as they were.
        """
        window = self.policy.first_review_window
        if window is None:
            return OrbitSettlement()
        owing = and_(
            DiscoveryWatchRow.status.in_([item.value for item in REVIEWABLE]),
            DiscoveryWatchRow.next_orbit_review_at.is_not(None),
        )
        async with self.sessions.begin() as session:
            stale = await session.execute(
                update(DiscoveryWatchRow)
                .where(
                    owing,
                    DiscoveryWatchRow.orbit_checkpoint_index.is_(None),
                    DiscoveryWatchRow.first_seen_at < now - window,
                )
                .values(
                    next_orbit_review_at=None,
                    orbit_state=OrbitState.FIRST_REVIEW_SKIPPED_STALE.value,
                    orbit_state_at=now,
                    updated_at=now,
                )
            )
            deferred = await session.execute(
                update(DiscoveryWatchRow)
                .where(owing, DiscoveryWatchRow.orbit_checkpoint_index.is_not(None))
                .values(
                    next_orbit_review_at=None,
                    orbit_state=OrbitState.FOLLOW_UPS_DEFERRED.value,
                    orbit_state_at=now,
                    updated_at=now,
                )
            )
        return OrbitSettlement(
            skipped_stale=int(getattr(stale, "rowcount", 0) or 0),
            follow_ups_deferred=int(getattr(deferred, "rowcount", 0) or 0),
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

    async def backlog(self, now: datetime) -> "Backlog":
        """How far ORBIT is behind: due reviews, the oldest one, and unreviewed watches.

        Counts over the reviewable set only. DORMANT and RETIRED watches cause no
        traffic and are not work anybody owes.
        """
        reviewable = DiscoveryWatchRow.status.in_([item.value for item in REVIEWABLE])
        due = self._due(DiscoveryWatchRow.next_orbit_review_at, REVIEWABLE, now)
        async with self.sessions() as session:
            count = await session.scalar(
                select(func.count()).select_from(DiscoveryWatchRow).where(due)
            )
            oldest = await session.scalar(
                select(func.min(DiscoveryWatchRow.next_orbit_review_at)).where(due)
            )
            unreviewed = await session.scalar(
                select(func.count())
                .select_from(DiscoveryWatchRow)
                .where(
                    reviewable,
                    DiscoveryWatchRow.orbit_checkpoint_index.is_(None),
                    DiscoveryWatchRow.orbit_state.is_(None),
                )
            )
            grouped = (
                await session.execute(
                    select(DiscoveryWatchRow.orbit_state, func.count())
                    .where(DiscoveryWatchRow.orbit_state.is_not(None))
                    .group_by(DiscoveryWatchRow.orbit_state)
                )
            ).all()
            states: dict[str, int] = {str(state): int(total) for state, total in grouped}
        return Backlog(
            due=int(count or 0),
            oldest_due_age_seconds=None
            if oldest is None
            else max(0, int((now - aware(oldest)).total_seconds())),
            unreviewed=int(unreviewed or 0),
            skipped_stale=int(states.get(OrbitState.FIRST_REVIEW_SKIPPED_STALE.value, 0)),
            follow_ups_deferred=int(states.get(OrbitState.FOLLOW_UPS_DEFERRED.value, 0)),
            reviewed=int(states.get(OrbitState.REVIEWED.value, 0)),
        )

    async def young_watching(
        self, limit: int, *, seen_since: datetime, chain: str
    ) -> tuple[DiscoveryWatch, ...]:
        """WATCHING watches on one chain first seen no earlier than `seen_since`.

        Youngest first. A necessary condition only: a pool first seen inside the
        window may still be older on chain, which the early strategy decides
        from the chain-side creation time. Never ranked by market size.
        """
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(DiscoveryWatchRow)
                    .where(
                        DiscoveryWatchRow.status == WatchStatus.WATCHING.value,
                        DiscoveryWatchRow.chain == chain,
                        DiscoveryWatchRow.first_seen_at >= seen_since,
                    )
                    .order_by(DiscoveryWatchRow.first_seen_at.desc(), DiscoveryWatchRow.pair_id)
                    .limit(limit)
                )
            ).all()
            return tuple(_watch(row) for row in rows)

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

    async def declined(self, identities: list[MarketIdentity]) -> set[str]:
        """Pair ids among these streams that the watch limit already turned away."""
        if not identities:
            return set()
        row = DiscoveryStreamDeclineRow
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(row.provider, row.chain, row.network, row.pair_id, row.is_fixture).where(
                        row.pair_id.in_({identity.pair_id for identity in identities})
                    )
                )
            ).all()
        keys = {tuple(item) for item in rows}
        return {
            identity.pair_id
            for identity in identities
            if (
                identity.provider,
                identity.chain,
                identity.network,
                identity.pair_id,
                identity.is_fixture,
            )
            in keys
        }

    async def decline(self, snapshot: MarketSnapshot, *, now: datetime, reason: str) -> bool:
        """Mark a stream the watch limit turned away. Idempotent; True if new."""
        identity = snapshot.pair.market_identity
        values = dict(
            id=uuid4(),
            provider=identity.provider,
            chain=identity.chain,
            network=identity.network,
            pair_id=identity.pair_id,
            is_fixture=identity.is_fixture,
            reason=reason,
            declined_at=now,
        )
        async with self.sessions.begin() as session:
            dialect = session.get_bind().dialect.name
            insert = pg_insert if dialect == "postgresql" else sqlite_insert
            result = await session.execute(
                insert(DiscoveryStreamDeclineRow)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=["provider", "chain", "network", "pair_id", "is_fixture"]
                )
            )
            return bool(getattr(result, "rowcount", 0) == 1)

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
                    *(
                        # V2: only a pending first review still inside the window.
                        (self._fresh_first(now),) if self.policy.fresh_first_only else ()
                    ),
                )
                .values(
                    next_orbit_review_at=next_at,
                    orbit_checkpoint_index=checkpoint_index,
                    updated_at=now,
                    **(
                        {"orbit_state": OrbitState.REVIEWED.value, "orbit_state_at": now}
                        if self.policy.fresh_first_only
                        else {}
                    ),
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
            # A read that succeeded ends any retry state a failure had set.
            history_retry_not_before=None,
            history_failure_count=0,
            history_last_failure=None,
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

    # ------------------------------------------------------------ history queue

    def _history_eligible(self, now: datetime) -> Any:
        """Due at a checkpoint, still watched, and not inside a retry backoff."""
        return and_(
            self._due(
                DiscoveryWatchRow.next_history_review_at, frozenset({WatchStatus.WATCHING}), now
            ),
            or_(
                DiscoveryWatchRow.history_retry_not_before.is_(None),
                DiscoveryWatchRow.history_retry_not_before <= now,
            ),
        )

    async def history_lanes(
        self, now: datetime, window: timedelta, limit: int
    ) -> tuple[dict[str, list[DiscoveryWatch]], dict[str, list[DiscoveryWatch]]]:
        """Eligible history checks per chain: CURRENT newest-due first, CATCH-UP oldest first.

        Ordered by due time, then pair identity, and nothing else: no market
        figure, JEV answer or classification can move a watch in either lane.
        """
        if limit <= 0:
            return {}, {}
        eligible = self._history_eligible(now)
        recent = DiscoveryWatchRow.next_history_review_at >= now - window
        due = DiscoveryWatchRow.next_history_review_at
        current: dict[str, list[DiscoveryWatch]] = {}
        catch_up: dict[str, list[DiscoveryWatch]] = {}
        async with self.sessions() as session:
            chains = (
                await session.scalars(select(DiscoveryWatchRow.chain).where(eligible).distinct())
            ).all()
            for chain in chains:
                in_chain = and_(eligible, DiscoveryWatchRow.chain == chain)
                current[chain] = [
                    _watch(row)
                    for row in (
                        await session.scalars(
                            select(DiscoveryWatchRow)
                            .where(in_chain, recent)
                            .order_by(due.desc(), DiscoveryWatchRow.pair_id)
                            .limit(limit)
                        )
                    ).all()
                ]
                catch_up[chain] = [
                    _watch(row)
                    for row in (
                        await session.scalars(
                            select(DiscoveryWatchRow)
                            .where(in_chain, ~recent)
                            .order_by(due, DiscoveryWatchRow.pair_id)
                            .limit(limit)
                        )
                    ).all()
                ]
        return current, catch_up

    async def history_eligible_count(self, now: datetime) -> tuple[int, int | None]:
        """Eligible history checks now, and the age of the oldest due checkpoint."""
        async with self.sessions() as session:
            eligible = await session.scalar(
                select(func.count())
                .select_from(DiscoveryWatchRow)
                .where(self._history_eligible(now))
            )
            oldest = await session.scalar(
                select(func.min(DiscoveryWatchRow.next_history_review_at)).where(
                    self._due(
                        DiscoveryWatchRow.next_history_review_at,
                        frozenset({WatchStatus.WATCHING}),
                        now,
                    )
                )
            )
        return int(eligible or 0), (
            None if oldest is None else max(0, int((now - aware(oldest)).total_seconds()))
        )

    async def history_failed(
        self,
        watch_id: UUID,
        *,
        expected_next: datetime | None,
        failure: str,
        now: datetime,
        backoff: "Callable[[int], timedelta]",
        max_failures: int,
    ) -> timedelta | None:
        """Set a bounded retry-not-before after a failed read. The checkpoint stays.

        Returns the backoff applied, or None when the watch moved on meanwhile
        (another run settled it), in which case nothing is changed.
        """
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(DiscoveryWatchRow)
                .where(
                    DiscoveryWatchRow.id == watch_id,
                    DiscoveryWatchRow.status == WatchStatus.WATCHING.value,
                    DiscoveryWatchRow.next_history_review_at == expected_next,
                )
                .with_for_update()
            )
            if row is None:
                return None
            failures = min(int(row.history_failure_count or 0) + 1, max_failures)
            pause = backoff(failures)
            row.history_failure_count = failures
            row.history_retry_not_before = now + pause
            row.history_last_failure = failure[:80]
            row.reason_code = failure[:80]
            row.updated_at = now
        return pause

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

        Recovery only: streams recorded outside a scout run, or left without a
        watch by a run that stopped between recording and syncing. A stream the
        per-run watch limit declined is never adopted here.

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
        # A stream the watch limit turned away is not a recovery case. Adopting
        # it one run later would open more watches than the limit allows.
        declined = exists(
            select(DiscoveryStreamDeclineRow.id).where(
                DiscoveryStreamDeclineRow.provider == row.provider,
                DiscoveryStreamDeclineRow.chain == row.chain,
                DiscoveryStreamDeclineRow.network == row.network,
                DiscoveryStreamDeclineRow.pair_id == row.pair_id,
                DiscoveryStreamDeclineRow.is_fixture.is_(False),
            )
        )
        async with self.sessions() as session:
            streams = (
                await session.execute(
                    select(row.provider, row.chain, row.network, row.pair_id, first)
                    .where(row.is_fixture.is_(False), ~watched, ~declined)
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
