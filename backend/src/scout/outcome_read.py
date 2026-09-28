"""Read-only view of discovery outcomes against JEV, ORBIT and the watch decision.

Descriptive only. Nothing here feeds a decision: the numbers are for deciding,
later and by people, whether JEV or ORBIT carry signal about what markets do.

Everything is keyed by the stream identity, so candidates the watch limit
declined are counted exactly like watched ones. Bounded: at most `SAMPLE` of
the most recent labels per horizon are read.
"""

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.data.tables import (
    DiscoveryFastAssessmentRow,
    DiscoveryOutcomeSampleRow,
    DiscoveryStreamDeclineRow,
    DiscoveryStreamOutcomeRow,
    DiscoveryWatchAssessmentRow,
    DiscoveryWatchRow,
)
from src.runner.models import Immutable
from src.scout.outcomes import HORIZONS_MINUTES, SCHEMA_VERSION, StreamKey

SAMPLE = 5000
# The horizon the comparisons are reported on, and what counts as a top outcome
# there. Descriptive choices, stated in the response; not thresholds for action.
FOCUS_HORIZON = 1440
TOP_SHARE = 0.10


class HorizonView(Immutable):
    horizon_minutes: int
    labelled: int
    missing: int
    missing_reasons: dict[str, int]
    median_return_pct: float | None = None
    median_max_return_pct: float | None = None
    median_max_drawdown_pct: float | None = None
    survival_rate: float | None = None


class GroupView(Immutable):
    """One group's outcome at the focus horizon."""

    group: str
    labelled: int
    median_return_pct: float | None = None
    median_max_return_pct: float | None = None
    survival_rate: float | None = None


class SignalView(Immutable):
    """A JEV signal's mean among top outcomes versus the rest, at the focus horizon."""

    question: str
    type: str
    top_mean: float | None = None
    rest_mean: float | None = None
    # Share of top outcomes whose yes-probability was >= 0.5 (noul questions only).
    recall_top_return_at_half: float | None = None


class OutcomeSummary(Immutable):
    notice: Literal["LABELS ONLY - NO DECISION EFFECT"] = "LABELS ONLY - NO DECISION EFFECT"
    schema_version: int
    focus_horizon_minutes: int
    top_share: float
    candidates: int
    sampled: int
    by_status: dict[str, int]
    by_history_source: dict[str, int]
    liquidity_persistence_median: float | None = None
    horizons: tuple[HorizonView, ...]
    watched_vs_declined: tuple[GroupView, ...]
    by_chain: tuple[GroupView, ...]
    by_orbit: tuple[GroupView, ...]
    jev_signals: tuple[SignalView, ...]


def _median(values: list[Decimal]) -> float | None:
    return round(float(statistics.median(values)), 4) if values else None


def _rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _group(name: str, rows: list[Any]) -> GroupView:
    return GroupView(
        group=name,
        labelled=len(rows),
        median_return_pct=_median([r.return_pct for r in rows if r.return_pct is not None]),
        median_max_return_pct=_median(
            [r.max_return_pct for r in rows if r.max_return_pct is not None]
        ),
        survival_rate=_rate([bool(r.survived) for r in rows if r.survived is not None]),
    )


def _key(row: Any, provider: str = "provider") -> StreamKey:
    return (getattr(row, provider), row.chain, row.network, row.pair_id, row.is_fixture)


@dataclass(frozen=True)
class OutcomeReadService:
    sessions: async_sessionmaker[AsyncSession]

    async def summary(self) -> OutcomeSummary:
        outcome = DiscoveryStreamOutcomeRow
        sample = DiscoveryOutcomeSampleRow
        async with self.sessions() as session:
            watched = await self._keys(session, DiscoveryWatchRow)
            declined = await self._keys(session, DiscoveryStreamDeclineRow)
            by_status = await self._count(session, sample.status)
            by_source = await self._count(session, sample.history_source)
            liquidity = (
                await session.execute(
                    select(sample.liquidity_at_reference_usd, sample.liquidity_at_sample_usd)
                    .where(sample.schema_version == SCHEMA_VERSION)
                    .order_by(sample.sampled_at.desc())
                    .limit(SAMPLE)
                )
            ).all()
            rows = (
                (
                    await session.execute(
                        select(outcome)
                        .where(outcome.schema_version == SCHEMA_VERSION)
                        .order_by(outcome.computed_at.desc())
                        .limit(SAMPLE * len(HORIZONS_MINUTES))
                    )
                )
                .scalars()
                .all()
            )
            orbit = await self._first_orbit(session)
            jev = await self._jev(session)
        horizons = []
        for minutes in HORIZONS_MINUTES:
            items = [r for r in rows if r.horizon_minutes == minutes]
            labelled = [r for r in items if r.status == "LABELLED"]
            horizons.append(
                HorizonView(
                    horizon_minutes=minutes,
                    labelled=len(labelled),
                    missing=len(items) - len(labelled),
                    missing_reasons=dict(
                        Counter(r.missing_reason for r in items if r.missing_reason)
                    ),
                    median_return_pct=_median(
                        [r.return_pct for r in labelled if r.return_pct is not None]
                    ),
                    median_max_return_pct=_median(
                        [r.max_return_pct for r in labelled if r.max_return_pct is not None]
                    ),
                    median_max_drawdown_pct=_median(
                        [r.max_drawdown_pct for r in labelled if r.max_drawdown_pct is not None]
                    ),
                    survival_rate=_rate(
                        [bool(r.survived) for r in labelled if r.survived is not None]
                    ),
                )
            )
        focus = [r for r in rows if r.horizon_minutes == FOCUS_HORIZON and r.status == "LABELLED"]
        split: dict[str, list[Any]] = defaultdict(list)
        chains: dict[str, list[Any]] = defaultdict(list)
        by_class: dict[str, list[Any]] = defaultdict(list)
        for row in focus:
            key = _key(row)
            split[
                "watched" if key in watched else "declined" if key in declined else "other"
            ].append(row)
            chains[row.chain].append(row)
            by_class[orbit.get(key, "NOT_REVIEWED")].append(row)
        ratios = [
            float(now / then)
            for then, now in liquidity
            if then is not None and now is not None and then > 0
        ]
        return OutcomeSummary(
            schema_version=SCHEMA_VERSION,
            focus_horizon_minutes=FOCUS_HORIZON,
            top_share=TOP_SHARE,
            candidates=len(watched | declined),
            sampled=sum(by_status.values()),
            by_status=by_status,
            by_history_source=by_source,
            liquidity_persistence_median=round(statistics.median(ratios), 4) if ratios else None,
            horizons=tuple(horizons),
            watched_vs_declined=tuple(_group(name, split[name]) for name in sorted(split)),
            by_chain=tuple(_group(name, chains[name]) for name in sorted(chains)),
            by_orbit=tuple(_group(name, by_class[name]) for name in sorted(by_class)),
            jev_signals=self._signals(focus, jev),
        )

    @staticmethod
    def _signals(focus: list[Any], jev: dict[StreamKey, dict[str, Any]]) -> tuple[SignalView, ...]:
        """JEV-0 signals of the top outcomes (by maximum return) versus the rest."""
        scored = [
            (row, jev[_key(row)])
            for row in focus
            if row.max_return_pct is not None and _key(row) in jev
        ]
        if not scored:
            return ()
        scored.sort(key=lambda item: item[0].max_return_pct, reverse=True)
        cut = max(1, int(len(scored) * TOP_SHARE))
        top, rest = scored[:cut], scored[cut:]
        questions = sorted({name for _, answers in scored for name in answers})
        views = []
        for name in questions:
            kind = next(str(a[name].get("type")) for _, a in scored if name in a)
            if kind == "choice":
                continue

            def value(answers: dict[str, Any], question: str = name) -> float | None:
                answer = answers.get(question) or {}
                raw = answer.get("noul") if answer.get("type") == "noul" else answer.get("score")
                return float(raw) if isinstance(raw, int | float) else None

            top_values = [v for _, a in top if (v := value(a)) is not None]
            rest_values = [v for _, a in rest if (v := value(a)) is not None]
            views.append(
                SignalView(
                    question=name,
                    type=kind,
                    top_mean=round(statistics.fmean(top_values), 4) if top_values else None,
                    rest_mean=round(statistics.fmean(rest_values), 4) if rest_values else None,
                    recall_top_return_at_half=round(
                        sum(1 for v in top_values if v >= 0.5) / len(top_values), 4
                    )
                    if kind == "noul" and top_values
                    else None,
                )
            )
        return tuple(views)

    @staticmethod
    async def _keys(session: AsyncSession, model: Any) -> set[StreamKey]:
        rows = (
            await session.execute(
                select(model.provider, model.chain, model.network, model.pair_id, model.is_fixture)
            )
        ).all()
        return {tuple(row) for row in rows}

    @staticmethod
    async def _count(session: AsyncSession, column: Any) -> dict[str, int]:
        rows = (
            await session.execute(
                select(column, func.count())
                .where(DiscoveryOutcomeSampleRow.schema_version == SCHEMA_VERSION)
                .group_by(column)
            )
        ).all()
        return {str(key): int(count) for key, count in rows}

    @staticmethod
    async def _first_orbit(session: AsyncSession) -> dict[StreamKey, str]:
        """Each watched stream's first completed ORBIT classification."""
        watch, review = DiscoveryWatchRow, DiscoveryWatchAssessmentRow
        rows = (
            await session.execute(
                select(
                    watch.provider,
                    watch.chain,
                    watch.network,
                    watch.pair_id,
                    watch.is_fixture,
                    review.classification,
                    review.assessed_at,
                )
                .join(review, review.watch_id == watch.id)
                .where(review.status == "COMPLETED")
                .order_by(review.assessed_at)
            )
        ).all()
        first: dict[StreamKey, str] = {}
        for provider, chain, network, pair_id, fixture, classification, _ in rows:
            first.setdefault((provider, chain, network, pair_id, fixture), classification)
        return first

    @staticmethod
    async def _jev(session: AsyncSession) -> dict[StreamKey, dict[str, Any]]:
        fast = DiscoveryFastAssessmentRow
        rows = (
            await session.execute(
                select(
                    fast.market_provider,
                    fast.chain,
                    fast.network,
                    fast.pair_id,
                    fast.is_fixture,
                    fast.answers,
                ).where(fast.status == "COMPLETED")
            )
        ).all()
        return {
            (provider, chain, network, pair_id, fixture): answers
            for provider, chain, network, pair_id, fixture, answers in rows
            if answers
        }


__all__ = ["OutcomeReadService", "OutcomeSummary"]
