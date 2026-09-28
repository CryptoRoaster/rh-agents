"""Read-only analysis of the JEV shadow assessments. Nothing decides from it.

Every number here is descriptive: how many assessments ran, how they failed,
how fast they were, how their signals are distributed, and how they line up
with the Codex ORBIT classification the same watch received later. Outcome
labels do not exist yet, so the outcome view says so instead of guessing.

Bounded: the distributions are computed over the most recent `SAMPLE` settled
assessments, never over an unbounded table scan in Python.
"""

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.data.repository import aware
from src.data.tables import (
    DiscoveryWatchAssessmentRow,
    DiscoveryWatchFastAssessmentRow,
    DiscoveryWatchRow,
)
from src.runner.models import Immutable

SAMPLE = 5000


class QuestionSignal(Immutable):
    """One question's answers across the sample."""

    type: str
    answered: int
    # noul: mean yes-probability; score: mean score; choice: None.
    mean: float | None = None
    # choice: chosen option counts; score: counts by rounded level; noul: by decile band.
    distribution: dict[str, int]


class CodexComparison(Immutable):
    """Shadow signals grouped by the Codex ORBIT classification the watch got later."""

    classification: str
    watches: int
    mean_by_question: dict[str, float]


class ShadowSummary(Immutable):
    notice: Literal["SHADOW - NO TRADING EFFECT"] = "SHADOW - NO TRADING EFFECT"
    total: int
    by_status: dict[str, int]
    by_model_version: dict[str, int]
    failure_codes: dict[str, int]
    latency_ms_p50: float | None = None
    latency_ms_p95: float | None = None
    sample: int
    signals: dict[str, QuestionSignal]
    by_chain: dict[str, int]
    versus_codex: tuple[CodexComparison, ...]
    outcomes: Literal["NOT_YET_LABELLED"] = "NOT_YET_LABELLED"


def _percentile(values: list[int], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    low, high = int(k), min(int(k) + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (k - low), 1)


def _value(answer: dict[str, object]) -> float | None:
    kind = answer.get("type")
    raw = answer.get("noul") if kind == "noul" else answer.get("score") if kind == "score" else None
    return float(raw) if isinstance(raw, int | float) else None


def _bucket(answer: dict[str, object]) -> str:
    kind = answer.get("type")
    if kind == "choice":
        return str(answer.get("choice"))
    value = _value(answer)
    if value is None:
        return "unknown"
    if kind == "score":
        return str(round(value))
    return f"{min(int(value * 10), 9) / 10:.1f}"


@dataclass(frozen=True)
class ShadowReadService:
    sessions: async_sessionmaker[AsyncSession]

    async def summary(self) -> ShadowSummary:
        row = DiscoveryWatchFastAssessmentRow
        async with self.sessions() as session:
            total = int(await session.scalar(select(func.count()).select_from(row)) or 0)
            by_status = await self._group(session, row.status)
            by_version = await self._group(session, func.coalesce(row.model_version, row.model))
            failures = await self._group(session, row.failure_reason_code, row.status == "FAILED")
            recent = (
                await session.execute(
                    select(row.watch_id, row.reserved_at, row.answers, row.latency_ms, row.status)
                    .where(row.status != "PENDING")
                    .order_by(row.reserved_at.desc(), row.id.desc())
                    .limit(SAMPLE)
                )
            ).all()
            watch_ids = [item.watch_id for item in recent]
            chain_rows = (
                await session.execute(
                    select(DiscoveryWatchRow.id, DiscoveryWatchRow.chain).where(
                        DiscoveryWatchRow.id.in_(watch_ids)
                    )
                )
            ).all()
            chains: dict[UUID, str] = {watch_id: chain for watch_id, chain in chain_rows}
            codex = await self._codex_reviews(session, watch_ids)
        latencies = [item.latency_ms for item in recent if item.latency_ms is not None]
        values: dict[str, list[float]] = defaultdict(list)
        buckets: dict[str, Counter[str]] = defaultdict(Counter)
        kinds: dict[str, str] = {}
        grouped: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        for item in recent:
            if item.status != "COMPLETED" or not item.answers:
                continue
            # The first ORBIT classification strictly after this shadow assessment.
            reserved = aware(item.reserved_at)
            later = next(
                (review for review in codex.get(item.watch_id, ()) if review[0] > reserved), None
            )
            for name, answer in item.answers.items():
                kinds[name] = str(answer.get("type"))
                buckets[name][_bucket(answer)] += 1
                value = _value(answer)
                if value is not None:
                    values[name].append(value)
                    if later is not None:
                        grouped[later[1]][name].append(value)
        signals = {
            name: QuestionSignal(
                type=kinds[name],
                answered=sum(buckets[name].values()),
                mean=round(statistics.fmean(values[name]), 4) if values[name] else None,
                distribution=dict(sorted(buckets[name].items())),
            )
            for name in sorted(kinds)
        }
        versus = tuple(
            CodexComparison(
                classification=classification,
                watches=max((len(items) for items in by_question.values()), default=0),
                mean_by_question={
                    name: round(statistics.fmean(items), 4)
                    for name, items in sorted(by_question.items())
                },
            )
            for classification, by_question in sorted(grouped.items())
        )
        return ShadowSummary(
            total=total,
            by_status=by_status,
            by_model_version=by_version,
            failure_codes=failures,
            latency_ms_p50=_percentile(latencies, 0.5),
            latency_ms_p95=_percentile(latencies, 0.95),
            sample=len(recent),
            signals=signals,
            by_chain=dict(Counter(chains.get(item.watch_id, "unknown") for item in recent)),
            versus_codex=versus,
        )

    @staticmethod
    async def _group(
        session: AsyncSession, column: Any, *where: ColumnElement[bool]
    ) -> dict[str, int]:
        query = select(column, func.count()).select_from(DiscoveryWatchFastAssessmentRow)
        for condition in where:
            query = query.where(condition)
        rows = (await session.execute(query.group_by(column))).all()
        return {str(key): int(count) for key, count in rows if key is not None}

    @staticmethod
    async def _codex_reviews(
        session: AsyncSession, watch_ids: list[UUID]
    ) -> dict[UUID, list[tuple[datetime, str]]]:
        """Each watch's completed ORBIT reviews, oldest first: (assessed_at, classification)."""
        if not watch_ids:
            return {}
        orbit = DiscoveryWatchAssessmentRow
        rows = (
            await session.execute(
                select(orbit.watch_id, orbit.assessed_at, orbit.classification)
                .where(orbit.watch_id.in_(watch_ids), orbit.status == "COMPLETED")
                .order_by(orbit.watch_id, orbit.assessed_at)
            )
        ).all()
        reviews: dict[UUID, list[tuple[datetime, str]]] = defaultdict(list)
        for watch_id, assessed_at, classification in rows:
            reviews[watch_id].append((aware(assessed_at), classification))
        return reviews
