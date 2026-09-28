"""JEV-0: one shadow fast assessment for every new discovery candidate.

A candidate is a valid, newly discovered market stream: no watch existed for
it and it had never been observed before this run. The candidate set is fixed
from discovery alone, *before* watch allocation, so a stream the watch limit
turns away is assessed exactly like one that becomes a watch. Assessments are
evidence about the stream (the existing provider/chain/network/pair identity)
and the observation it was shown, never about a watch.

**Shadow only.** Nothing in the scout, the watch allocation, ORBIT, VECTOR,
promotion, risk or execution reads what is written here. The calls run last
in a scout run, after every review and check, and their outcome — answers,
failure or a spent budget — changes no watch, no schedule and no queue. The
purpose is calibration: against the watch decision, against Codex ORBIT
classifications, and later against objective market outcomes.

The input is deterministic and uses only what was observed by the assessment
instant, with all arithmetic done here in code (Jev is not a calculator). No
symbol, name or address is shown. Magnitude bands describe a value; they are
not a gate.

Budget: its own, separate from ORBIT. A row is reserved (PENDING) and committed
before the call and settled exactly once, so a crashed call still counts
toward the UTC day and a stream is never asked twice for one question set.
"""

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.numbers import canonical_decimal
from src.data.repository import aware
from src.data.tables import (
    DiscoveryFastAssessmentRow,
    DiscoveryWatchRow,
    MarketObservationRow,
)
from src.fast_reasoning.models import (
    ChoiceQuestion,
    FastRequest,
    FastResult,
    NoulQuestion,
    Question,
    ScoreQuestion,
)
from src.fast_reasoning.provider import FastAssessmentProvider
from src.markets.models import Availability, MarketIdentity, MarketSnapshot, Measurement
from src.reasoning.models import Identifier, ReasoningErrorCategory, ReasoningFailure
from src.runner.models import Code, Immutable

QUESTION_VERSION = "jev-scout-v1"
INPUT_SCHEMA_VERSION = 1
BUDGET_LOCK = "rh-agents:scout-fast-budget"

# Atomic questions about the observed state. None asks what to do with the
# market; there is no buy, sell or trade question and there never is one here.
QUESTIONS: dict[str, Question] = {
    "data_quality": ChoiceQuestion(
        instructions=(
            "How complete is the observed data in this state? Look at the status of "
            "`price_usd`, `liquidity_usd` and `volume_usd`, and at `data_gaps`."
        ),
        criteria={
            "complete": "Price, liquidity and volume are all available.",
            "partial": "One or two of price, liquidity and volume are missing.",
            "insufficient": "Too little is observed to describe this market at all.",
        },
    ),
    "organic_activity": ScoreQuestion(
        instructions=(
            "Judging only `volume_usd`, `liquidity_usd` and `volume_to_liquidity`, how "
            "organic does the trading in this newly listed pool look?"
        ),
        criteria=(
            "No trading activity is visible.",
            "Activity looks thin, or artificial relative to the pool's size.",
            "Activity looks like plausible organic trading.",
            "Activity looks like strong, broad organic trading.",
        ),
    ),
    "suspicious_activity": NoulQuestion(
        instructions=(
            "Does this state suggest wash trading or manipulation, for example a "
            "`volume_to_liquidity` value that is many times larger than one?"
        ),
        criteria={
            "true": "The numbers look like wash trading or manipulation.",
            "false": "Nothing in the numbers suggests wash trading or manipulation.",
        },
    ),
    "anomaly_signal": NoulQuestion(
        instructions="Is anything in this state unusual for a newly listed DEX pool?",
        criteria={
            "true": "Something in the state is unusual for a new pool.",
            "false": "The state looks ordinary for a new pool.",
        },
    ),
    "momentum_quality": ScoreQuestion(
        instructions=(
            "Using only `prior_observation`, how is this pool developing since it was "
            "observed before? If `prior_observation.available` is false, choose the first level."
        ),
        criteria=(
            "There is no earlier observation to compare with.",
            "Price, liquidity or volume are falling.",
            "Mixed or flat development.",
            "Price, liquidity and volume are rising together.",
        ),
    ),
    "continuation_signal": NoulQuestion(
        instructions=(
            "Judging only from this state, is trading in this pool likely to continue over "
            "the next hours?"
        ),
        criteria={
            "true": "Trading is likely to continue.",
            "false": "Trading is likely to fade out.",
        },
    ),
}

FastStatus = Literal["PENDING", "COMPLETED", "FAILED"]


class FastAssessment(Immutable):
    """One persisted shadow assessment, as the cockpit reads it."""

    id: UUID
    market_provider: Identifier
    chain: Identifier
    network: Identifier
    pair_id: str
    is_fixture: bool
    snapshot_id: UUID
    reserved_at: AwareDatetime
    assessed_at: AwareDatetime | None = None
    status: FastStatus
    provider: Identifier
    model: Identifier
    model_version: Identifier | None = None
    question_version: Identifier
    input_schema_version: int
    input_digest: str
    input_payload: dict[str, Any]
    answers: dict[str, Any] | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    failure_category: Code | None = None
    failure_reason_code: Code | None = None


def _fast(row: DiscoveryFastAssessmentRow) -> FastAssessment:
    return FastAssessment(
        id=row.id,
        market_provider=row.market_provider,
        chain=row.chain,
        network=row.network,
        pair_id=row.pair_id,
        is_fixture=row.is_fixture,
        snapshot_id=row.snapshot_id,
        reserved_at=aware(row.reserved_at),
        assessed_at=None if row.assessed_at is None else aware(row.assessed_at),
        status=row.status,  # type: ignore[arg-type]
        provider=row.provider,
        model=row.model,
        model_version=row.model_version,
        question_version=row.question_version,
        input_schema_version=row.input_schema_version,
        input_digest=row.input_digest,
        input_payload=row.input_payload,
        answers=row.answers,
        latency_ms=row.latency_ms,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        failure_category=row.failure_category,
        failure_reason_code=row.failure_reason_code,
    )


# ------------------------------------------------------------------ input


BANDS = (
    (Decimal("1000"), "under 1k USD"),
    (Decimal("10000"), "1k to 10k USD"),
    (Decimal("100000"), "10k to 100k USD"),
    (Decimal("1000000"), "100k to 1M USD"),
    (Decimal("10000000"), "1M to 10M USD"),
)


def magnitude(value: Decimal | None) -> str | None:
    """A description of size, for a reader that should not do arithmetic. Not a gate."""
    if value is None:
        return None
    for bound, label in BANDS:
        if value < bound:
            return label
    return "over 10M USD"


def _measure(item: Measurement) -> dict[str, object]:
    available = item.status == Availability.AVAILABLE
    return {
        "status": item.status.value,
        "value": canonical_decimal(item.value_usd)
        if available and item.value_usd is not None
        else None,
    }


def _change(new: Decimal | None, old: Decimal | None) -> str | None:
    if new is None or old is None or old == 0:
        return None
    return canonical_decimal(((new - old) / old * 100).quantize(Decimal("0.1")))


def _value(item: Measurement) -> Decimal | None:
    return item.value_usd if item.status == Availability.AVAILABLE else None


def build_input(
    snapshot: MarketSnapshot,
    prior: MarketSnapshot | None,
    first_seen_at: datetime,
    now: datetime,
) -> dict[str, object]:
    """The deterministic state shown to the fast model. Nothing after `now`.

    Built from the stream's observations alone — no watch, ORBIT or case
    state — so a candidate the watch limit turned away is described exactly
    like one that became a watch. `first_seen_at` is the stream's first
    observation instant.
    """
    liquidity = _value(snapshot.liquidity)
    volume = _value(snapshot.volume)
    ratio = (
        canonical_decimal((volume / liquidity).quantize(Decimal("0.01")))
        if volume is not None and liquidity
        else None
    )
    gaps = [
        f"{name}_{item.status.value.lower()}"
        for name, item in (
            ("price", snapshot.price),
            ("liquidity", snapshot.liquidity),
            ("volume", snapshot.volume),
        )
        if item.status != Availability.AVAILABLE
    ]
    earlier: dict[str, object] = {"available": False}
    if prior is not None and prior.observed_at < snapshot.observed_at:
        earlier = {
            "available": True,
            "minutes_before": int((snapshot.observed_at - prior.observed_at).total_seconds() // 60),
            "price_change_pct": _change(_value(snapshot.price), _value(prior.price)),
            "liquidity_change_pct": _change(liquidity, _value(prior.liquidity)),
            "volume_change_pct": _change(volume, _value(prior.volume)),
        }
    return {
        "schema_version": INPUT_SCHEMA_VERSION,
        "chain": snapshot.chain,
        "venue": snapshot.pair.venue,
        "stream_age_minutes": max(0, int((now - first_seen_at).total_seconds() // 60)),
        "observation_age_seconds": max(0, int((now - snapshot.freshness_at).total_seconds())),
        "price_usd": _measure(snapshot.price),
        "liquidity_usd": {**_measure(snapshot.liquidity), "magnitude": magnitude(liquidity)},
        "volume_usd": {
            **_measure(snapshot.volume),
            "window_hours": snapshot.volume.window_seconds // 3600,
            "magnitude": magnitude(volume),
        },
        "volume_to_liquidity": ratio,
        "prior_observation": earlier,
        "data_gaps": gaps,
    }


def input_digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


# ------------------------------------------------------------------ store


def stream_key(identity: MarketIdentity) -> tuple[Any, ...]:
    row = DiscoveryFastAssessmentRow
    return (
        row.market_provider == identity.provider,
        row.chain == identity.chain,
        row.network == identity.network,
        row.pair_id == identity.pair_id,
        row.is_fixture.is_(identity.is_fixture),
    )


def utc_day(instant: datetime) -> date:
    return instant.astimezone(UTC).date()


@dataclass(frozen=True)
class FastAssessmentStore:
    """Durable shadow assessments: reserve before the call, settle once."""

    sessions: async_sessionmaker[AsyncSession]

    async def used(self, day: date) -> int:
        async with self.sessions() as session:
            return await self._count(session, day)

    @staticmethod
    async def _count(session: AsyncSession, day: date) -> int:
        count = await session.scalar(
            select(func.count())
            .select_from(DiscoveryFastAssessmentRow)
            .where(DiscoveryFastAssessmentRow.utc_day == day)
        )
        return int(count or 0)

    async def reserve(
        self,
        *,
        snapshot: MarketSnapshot,
        provider: str,
        model: str,
        payload: dict[str, object],
        now: datetime,
        cap: int,
    ) -> UUID | None:
        """A PENDING row, committed before any call; None when spent or already asked.

        One per stream and question set: a pool seen again in a later run,
        with or without a watch, is never asked a second time.
        """
        day = utc_day(now)
        identity = snapshot.pair.market_identity
        async with self.sessions.begin() as session:
            if session.get_bind().dialect.name == "postgresql":
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": BUDGET_LOCK}
                )
            asked = await session.scalar(
                select(DiscoveryFastAssessmentRow.id).where(
                    *stream_key(identity),
                    DiscoveryFastAssessmentRow.question_version == QUESTION_VERSION,
                )
            )
            if asked is not None or await self._count(session, day) >= cap:
                return None
            reservation = uuid4()
            session.add(
                DiscoveryFastAssessmentRow(
                    id=reservation,
                    market_provider=identity.provider,
                    chain=identity.chain,
                    network=identity.network,
                    pair_id=identity.pair_id,
                    is_fixture=identity.is_fixture,
                    snapshot_id=snapshot.id,
                    utc_day=day,
                    reserved_at=now,
                    assessed_at=None,
                    status="PENDING",
                    provider=provider,
                    model=model,
                    model_version=None,
                    question_version=QUESTION_VERSION,
                    input_schema_version=INPUT_SCHEMA_VERSION,
                    input_digest=input_digest(payload),
                    input_payload=payload,
                    answers=None,
                    latency_ms=None,
                    input_tokens=None,
                    output_tokens=None,
                    failure_category=None,
                    failure_reason_code=None,
                )
            )
        return reservation

    async def complete(self, reservation: UUID, result: FastResult, now: datetime) -> None:
        await self._settle(
            reservation,
            status="COMPLETED",
            assessed_at=now,
            model_version=result.model_version,
            answers={
                name: answer.model_dump(mode="json") for name, answer in result.answers.items()
            },
            latency_ms=result.latency_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )

    async def fail(
        self, reservation: UUID, *, category: str, code: str, now: datetime, latency_ms: int
    ) -> None:
        await self._settle(
            reservation,
            status="FAILED",
            assessed_at=now,
            failure_category=category,
            failure_reason_code=code,
            latency_ms=latency_ms,
        )

    async def _settle(self, reservation: UUID, **values: object) -> None:
        # Only a PENDING row moves, and only once: a settled assessment is history.
        async with self.sessions.begin() as session:
            await session.execute(
                update(DiscoveryFastAssessmentRow)
                .where(
                    DiscoveryFastAssessmentRow.id == reservation,
                    DiscoveryFastAssessmentRow.status == "PENDING",
                )
                .values(**values)
            )

    async def for_stream(
        self, provider: str, chain: str, network: str, pair_id: str, is_fixture: bool
    ) -> tuple[FastAssessment, ...]:
        """The stream's shadow assessments, oldest first. A watch finds its own by its key."""
        row = DiscoveryFastAssessmentRow
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(row)
                    .where(
                        row.market_provider == provider,
                        row.chain == chain,
                        row.network == network,
                        row.pair_id == pair_id,
                        row.is_fixture.is_(is_fixture),
                    )
                    .order_by(row.reserved_at, row.id)
                )
            ).all()
            return tuple(_fast(item) for item in rows)

    async def is_new_candidate(self, snapshot: MarketSnapshot) -> bool:
        """Never observed before this snapshot and without a watch: a new candidate."""
        identity = snapshot.pair.market_identity
        observation = MarketObservationRow
        async with self.sessions() as session:
            earlier = await session.scalar(
                select(observation.id)
                .where(
                    observation.provider == identity.provider,
                    observation.chain == identity.chain,
                    observation.network == identity.network,
                    observation.pair_id == identity.pair_id,
                    observation.is_fixture.is_(identity.is_fixture),
                    observation.observed_at < snapshot.observed_at,
                )
                .limit(1)
            )
            watched = await session.scalar(
                select(DiscoveryWatchRow.id).where(
                    DiscoveryWatchRow.provider == identity.provider,
                    DiscoveryWatchRow.chain == identity.chain,
                    DiscoveryWatchRow.network == identity.network,
                    DiscoveryWatchRow.pair_id == identity.pair_id,
                    DiscoveryWatchRow.is_fixture.is_(identity.is_fixture),
                )
            )
        return earlier is None and watched is None

    async def prior(self, snapshot: MarketSnapshot) -> MarketSnapshot | None:
        """The stream's newest observation strictly before this one, if any."""
        row = MarketObservationRow
        identity = snapshot.pair.market_identity
        async with self.sessions() as session:
            found = await session.scalar(
                select(row)
                .where(
                    row.provider == identity.provider,
                    row.chain == identity.chain,
                    row.network == identity.network,
                    row.pair_id == identity.pair_id,
                    row.is_fixture.is_(identity.is_fixture),
                    row.observed_at < snapshot.observed_at,
                )
                .order_by(row.observed_at.desc(), row.recorded_at.desc(), row.id.desc())
                .limit(1)
            )
        return None if found is None else MarketSnapshot.model_validate(found.payload)


# ------------------------------------------------------------------- run


@dataclass
class ShadowTally:
    started: int = 0
    completed: int = 0
    failed: int = 0
    skipped_budget: int = 0
    failure_codes: dict[str, int] = field(default_factory=dict)

    def failure(self, code: str) -> None:
        self.failed += 1
        self.failure_codes[code] = self.failure_codes.get(code, 0) + 1


@dataclass(frozen=True)
class ShadowTriage:
    """Ask the fast provider once about each watch this run opened, and record it."""

    store: FastAssessmentStore
    provider: FastAssessmentProvider
    per_run: int
    per_day: int

    async def run(
        self,
        candidates: list[MarketSnapshot],
        now_fn: Callable[[], datetime],
        safe: Callable[[str], str],
    ) -> ShadowTally:
        """Assess the candidates fixed from discovery, in discovery order, within budget."""
        tally = ShadowTally()
        for snapshot in candidates[: max(0, self.per_run)]:
            now = now_fn()
            payload = build_input(snapshot, None, snapshot.observed_at, now)
            reservation = await self.store.reserve(
                snapshot=snapshot,
                provider=self.provider.name,
                model=self.provider.model,
                payload=payload,
                now=now,
                cap=self.per_day,
            )
            if reservation is None:
                tally.skipped_budget += 1
                continue
            tally.started += 1
            started = asyncio.get_running_loop().time()
            try:
                result = await self.provider.assess(FastRequest(state=payload, questions=QUESTIONS))
            except ReasoningFailure as error:
                code = safe(error.reason_code)
                await self.store.fail(
                    reservation,
                    category=error.category.value,
                    code=code,
                    now=now_fn(),
                    latency_ms=int((asyncio.get_running_loop().time() - started) * 1000),
                )
                tally.failure(code)
                continue
            except Exception:  # noqa: BLE001 - a shadow failure must never become the run's
                await self.store.fail(
                    reservation,
                    category=ReasoningErrorCategory.PROVIDER_UNAVAILABLE.value,
                    code="FAST_PROVIDER_INTERNAL_ERROR",
                    now=now_fn(),
                    latency_ms=int((asyncio.get_running_loop().time() - started) * 1000),
                )
                tally.failure("FAST_PROVIDER_INTERNAL_ERROR")
                continue
            await self.store.complete(reservation, result, now_fn())
            tally.completed += 1
        return tally
