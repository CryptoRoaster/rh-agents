"""Read-only views of scout state for the cockpit.

Everything here reads; nothing writes, calls a provider or asks a model. The
list order is a presentation order — newest discovery first — and has no bearing
on the order in which the scout works through due watches, which stays
`next_orbit_review_at`, `first_seen_at`, `pair_id`.

Nothing is hidden for being old. A watch whose latest reading is stale or whose
price is unknown is shown with that reading and its availability exactly as
recorded; `/api/markets`, which serves only fresh available markets, is the
wrong source for a watch list and is not used.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field
from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.data.repository import aware
from src.data.tables import (
    DiscoveryWatchAssessmentRow,
    DiscoveryWatchRow,
    MarketObservationRow,
    TradeCaseRow,
)
from src.markets.models import MarketSnapshot
from src.runner.models import Code, Identifier, Immutable
from src.scout.models import DiscoveryWatch, ScoutRun, WatchAssessment
from src.scout.policy import EARLY_SCOUT_V1, REVIEWABLE, EarlyScoutPolicy, WatchStatus
from src.scout.repository import Backlog, WatchRepository, _watch
from src.scout.runs import ScoutRunRepository

Availability = Literal["AVAILABLE", "UNKNOWN", "UNAVAILABLE"]


class SnapshotView(Immutable):
    """The watch's latest recorded reading, availability kept exactly."""

    snapshot_id: UUID
    observed_at: AwareDatetime
    base_symbol: Identifier
    quote_symbol: Identifier
    price_usd: Decimal | None = None
    price_status: Availability
    liquidity_usd: Decimal | None = None
    liquidity_status: Availability
    volume_usd: Decimal | None = None
    volume_status: Availability
    volume_window_seconds: int


class AssessmentBrief(Immutable):
    assessed_at: AwareDatetime
    checkpoint_seconds: int
    status: Literal["COMPLETED", "FAILED"]
    classification: Code | None = None
    strength: Code | None = None


class WatchView(Immutable):
    watch_id: UUID
    provider: Identifier
    chain: Identifier
    network: Identifier
    pair_id: Identifier
    venue: Identifier
    base_asset_id: Identifier
    quote_asset_id: Identifier
    is_fixture: bool
    first_seen_at: AwareDatetime
    last_seen_at: AwareDatetime
    age_seconds: int
    status: WatchStatus
    reason_code: Code
    next_orbit_review_at: AwareDatetime | None = None
    orbit_checkpoint_index: int | None = None
    next_history_review_at: AwareDatetime | None = None
    latest_vector_sufficiency: Code | None = None
    vector_checked_at: AwareDatetime | None = None
    last_promoted_trade_case_id: UUID | None = None
    latest_snapshot: SnapshotView | None = None
    latest_assessment: AssessmentBrief | None = None
    assessments: int = 0


class WatchPage(Immutable):
    items: tuple[WatchView, ...]
    total: int
    limit: int
    offset: int


class CheckpointView(Immutable):
    """One ORBIT checkpoint and what actually happened at it.

    ASSESSED / FAILED: a review was taken for this checkpoint.
    COALESCED: never reviewed on its own — a later checkpoint's review covered
    it, because the scout does not replay missed checkpoints.
    DUE: its time has passed and no review has been taken yet.
    PENDING: still in the future.
    NOT_SCHEDULED: the watch is DORMANT or RETIRED and will not be reviewed.
    """

    checkpoint_index: int
    checkpoint_seconds: int
    due_at: AwareDatetime
    state: Literal["ASSESSED", "FAILED", "COALESCED", "DUE", "PENDING", "NOT_SCHEDULED"]
    assessment_id: UUID | None = None


class LinkedCase(Immutable):
    trade_case_id: UUID
    status: Code
    opened_at: AwareDatetime


class WatchDetail(Immutable):
    watch: WatchView
    checkpoints: tuple[CheckpointView, ...]
    assessments: tuple[WatchAssessment, ...]
    trade_case: LinkedCase | None = None


class RunView(Immutable):
    run: ScoutRun
    duration_seconds: float
    identity_acceptance_rate: float | None = None
    watch_creation_rate: float | None = None


class RunPage(Immutable):
    items: tuple[RunView, ...]
    total: int
    limit: int
    offset: int


class ScoutOverview(Immutable):
    """Cockpit KPIs at one instant."""

    as_of: AwareDatetime
    policy_version: Identifier
    watches: int
    by_status: dict[str, int]
    orbit_backlog: int
    oldest_orbit_due_age_seconds: int | None = None
    unreviewed_watches: int
    latest_run: RunView | None = None
    very_young_seconds: int = Field(default=21600)


def snapshot_view(snapshot: MarketSnapshot) -> SnapshotView:
    return SnapshotView(
        snapshot_id=snapshot.id,
        observed_at=snapshot.observed_at,
        base_symbol=snapshot.pair.base.symbol,
        quote_symbol=snapshot.pair.quote.symbol,
        price_usd=snapshot.price.value_usd,
        price_status=snapshot.price.status.value,
        liquidity_usd=snapshot.liquidity.value_usd,
        liquidity_status=snapshot.liquidity.status.value,
        volume_usd=snapshot.volume.value_usd,
        volume_status=snapshot.volume.status.value,
        volume_window_seconds=snapshot.volume.window_seconds,
    )


def run_view(run: ScoutRun) -> RunView:
    return RunView(
        run=run,
        duration_seconds=(run.completed_at - run.started_at).total_seconds(),
        identity_acceptance_rate=run.identity_acceptance_rate,
        watch_creation_rate=run.watch_creation_rate,
    )


def checkpoint_plan(
    watch: DiscoveryWatch,
    assessments: tuple[WatchAssessment, ...],
    now: datetime,
    policy: EarlyScoutPolicy = EARLY_SCOUT_V1,
) -> tuple[CheckpointView, ...]:
    """What happened at every ORBIT checkpoint, derived from the stored facts."""
    taken = {item.checkpoint_index: item for item in assessments}
    latest = max(taken, default=-1)
    plan = []
    for index, offset in enumerate(policy.orbit_checkpoints):
        due_at = watch.first_seen_at + offset
        found = taken.get(index)
        if found is not None:
            state = "ASSESSED" if found.status == "COMPLETED" else "FAILED"
        elif index < latest:
            state = "COALESCED"
        elif watch.status not in REVIEWABLE:
            state = "NOT_SCHEDULED"
        elif due_at <= now:
            state = "DUE"
        else:
            state = "PENDING"
        plan.append(
            CheckpointView(
                checkpoint_index=index,
                checkpoint_seconds=int(offset.total_seconds()),
                due_at=due_at,
                state=state,  # type: ignore[arg-type]
                assessment_id=None if found is None else found.id,
            )
        )
    return tuple(plan)


@dataclass(frozen=True)
class ScoutReadService:
    sessions: async_sessionmaker[AsyncSession]
    policy: EarlyScoutPolicy = EARLY_SCOUT_V1

    async def watches(
        self,
        now: datetime,
        *,
        status: WatchStatus | None = None,
        chain: str | None = None,
        venue: str | None = None,
        has_assessment: bool | None = None,
        promotable: bool | None = None,
        max_age: timedelta | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> WatchPage:
        row = DiscoveryWatchRow
        conditions = []
        if status is not None:
            conditions.append(row.status == status.value)
        if promotable is not None:
            conditions.append(
                (row.status == WatchStatus.PROMOTABLE.value)
                if promotable
                else (row.status != WatchStatus.PROMOTABLE.value)
            )
        if chain is not None:
            conditions.append(row.chain == chain)
        if venue is not None:
            conditions.append(row.market_payload["venue"].as_string() == venue)
        if has_assessment is not None:
            assessed = exists(
                select(DiscoveryWatchAssessmentRow.id).where(
                    DiscoveryWatchAssessmentRow.watch_id == row.id
                )
            )
            conditions.append(assessed if has_assessment else ~assessed)
        if max_age is not None:
            conditions.append(row.first_seen_at >= now - max_age)
        where = and_(*conditions) if conditions else and_(True)
        async with self.sessions() as session:
            total = await session.scalar(select(func.count()).select_from(row).where(where))
            rows = (
                await session.scalars(
                    select(row)
                    .where(where)
                    .order_by(row.first_seen_at.desc(), row.pair_id)
                    .limit(limit)
                    .offset(offset)
                )
            ).all()
            watches = [_watch(item) for item in rows]
            views = await self._views(session, watches, now)
        return WatchPage(items=views, total=int(total or 0), limit=limit, offset=offset)

    async def _views(
        self, session: AsyncSession, watches: list[DiscoveryWatch], now: datetime
    ) -> tuple[WatchView, ...]:
        if not watches:
            return ()
        snapshot_ids = [item.latest_snapshot_id for item in watches]
        observations = {
            item.id: MarketSnapshot.model_validate(item.payload)
            for item in (
                await session.scalars(
                    select(MarketObservationRow).where(MarketObservationRow.id.in_(snapshot_ids))
                )
            ).all()
        }
        ids = [item.id for item in watches]
        assessment_rows = (
            await session.scalars(
                select(DiscoveryWatchAssessmentRow)
                .where(DiscoveryWatchAssessmentRow.watch_id.in_(ids))
                .order_by(
                    DiscoveryWatchAssessmentRow.assessed_at,
                    DiscoveryWatchAssessmentRow.checkpoint_index,
                )
            )
        ).all()
        latest: dict[UUID, DiscoveryWatchAssessmentRow] = {}
        counts: dict[UUID, int] = {}
        for item in assessment_rows:
            latest[item.watch_id] = item
            counts[item.watch_id] = counts.get(item.watch_id, 0) + 1
        return tuple(
            self._view(
                watch,
                observations.get(watch.latest_snapshot_id),
                latest.get(watch.id),
                counts.get(watch.id, 0),
                now,
            )
            for watch in watches
        )

    @staticmethod
    def _view(
        watch: DiscoveryWatch,
        snapshot: MarketSnapshot | None,
        latest: DiscoveryWatchAssessmentRow | None,
        count: int,
        now: datetime,
    ) -> WatchView:
        return WatchView(
            watch_id=watch.id,
            provider=watch.provider,
            chain=watch.chain,
            network=watch.network,
            pair_id=watch.pair_id,
            venue=watch.market.venue,
            base_asset_id=watch.market.base_asset_id,
            quote_asset_id=watch.market.quote_asset_id,
            is_fixture=watch.is_fixture,
            first_seen_at=watch.first_seen_at,
            last_seen_at=watch.last_seen_at,
            age_seconds=watch.age_seconds(now),
            status=watch.status,
            reason_code=watch.reason_code,
            next_orbit_review_at=watch.next_orbit_review_at,
            orbit_checkpoint_index=watch.orbit_checkpoint_index,
            next_history_review_at=watch.next_history_review_at,
            latest_vector_sufficiency=watch.latest_vector_sufficiency,
            vector_checked_at=watch.vector_checked_at,
            last_promoted_trade_case_id=watch.last_promoted_trade_case_id,
            latest_snapshot=None if snapshot is None else snapshot_view(snapshot),
            latest_assessment=None
            if latest is None
            else AssessmentBrief(
                assessed_at=aware(latest.assessed_at),
                checkpoint_seconds=latest.checkpoint_seconds,
                status=latest.status,  # type: ignore[arg-type]
                classification=latest.classification,
                strength=latest.strength,
            ),
            assessments=count,
        )

    async def detail(self, watch_id: UUID, now: datetime) -> WatchDetail | None:
        repository = WatchRepository(self.sessions, policy=self.policy)
        watch = await repository.get(watch_id)
        if watch is None:
            return None
        assessments = await repository.assessments(watch_id)
        async with self.sessions() as session:
            (view,) = await self._views(session, [watch], now)
            linked = None
            if watch.last_promoted_trade_case_id is not None:
                case = await session.get(TradeCaseRow, watch.last_promoted_trade_case_id)
                if case is not None:
                    linked = LinkedCase(
                        trade_case_id=case.id, status=case.status, opened_at=aware(case.opened_at)
                    )
        return WatchDetail(
            watch=view,
            checkpoints=checkpoint_plan(watch, assessments, now, self.policy),
            assessments=assessments,
            trade_case=linked,
        )

    async def assessments(self, watch_id: UUID) -> tuple[WatchAssessment, ...] | None:
        repository = WatchRepository(self.sessions, policy=self.policy)
        if await repository.get(watch_id) is None:
            return None
        return await repository.assessments(watch_id)

    async def runs(self, limit: int = 20, offset: int = 0) -> RunPage:
        repository = ScoutRunRepository(self.sessions)
        items = await repository.recent(limit, offset)
        return RunPage(
            items=tuple(run_view(item) for item in items),
            total=await repository.count(),
            limit=limit,
            offset=offset,
        )

    async def overview(self, now: datetime) -> ScoutOverview:
        backlog: Backlog = await WatchRepository(self.sessions, policy=self.policy).backlog(now)
        async with self.sessions() as session:
            grouped = (
                await session.execute(
                    select(DiscoveryWatchRow.status, func.count()).group_by(
                        DiscoveryWatchRow.status
                    )
                )
            ).all()
        by_status = {item.value: 0 for item in WatchStatus}
        for status, count in grouped:
            by_status[status] = int(count)
        (latest,) = (await ScoutRunRepository(self.sessions).recent(1)) or (None,)
        return ScoutOverview(
            as_of=now,
            policy_version=self.policy.version,
            watches=sum(by_status.values()),
            by_status=by_status,
            orbit_backlog=backlog.due,
            oldest_orbit_due_age_seconds=backlog.oldest_due_age_seconds,
            unreviewed_watches=backlog.unreviewed,
            latest_run=None if latest is None else run_view(latest),
        )
