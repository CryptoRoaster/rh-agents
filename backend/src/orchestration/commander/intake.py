"""Deterministic autonomous case intake. Operational, never strategic.

A TradeCase could previously only be opened by test code: nothing in `src/`
calls `open_trade_case`, and every API route is a read. So an autonomous system
had no way to begin, which is the gap this closes.

What it deliberately does not do is choose. There is no ranking by liquidity, no
momentum filter, no hype threshold and no score — ORBIT exists precisely to
judge whether a candidate is worth pursuing, and a coordinator that pre-filtered
on market grounds would be a second, invisible analyst whose reasoning nobody
recorded. Intake decides only whether the *machinery* may open a case: a
supported chain, a valid canonical identity, a fresh observation, no active
duplicate, and a bounded number per cycle.

Every opened case then goes through the ordinary workflow, and ORBIT is the
first thing that looks at it.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.data.tables import TradeCaseRow
from src.markets.models import MarketCandidate, MarketSnapshot
from src.orchestration.commander.context import SystemPausePort
from src.orchestration.commander.policy import COMMANDER_CONTROL_V1, CommanderControlPolicy
from src.orchestration.workflow.models import (
    TERMINAL_CASE_STATUSES,
    TradeCase,
)
from src.orchestration.workflow.service import TradeCaseService

logger = logging.getLogger(__name__)


class IntakeRefusal(StrEnum):
    """Why a candidate did not open a case. Typed, so the answer is auditable."""

    CHAIN_NOT_ENABLED = "CHAIN_NOT_ENABLED"
    FIXTURE_MARKET = "FIXTURE_MARKET"
    CANDIDATE_TOO_OLD = "CANDIDATE_TOO_OLD"
    MARKET_UNAVAILABLE = "MARKET_UNAVAILABLE"
    IDENTITY_INVALID = "IDENTITY_INVALID"
    ACTIVE_CASE_EXISTS = "ACTIVE_CASE_EXISTS"
    CYCLE_LIMIT_REACHED = "CYCLE_LIMIT_REACHED"
    SYSTEM_PAUSED = "SYSTEM_PAUSED"


@dataclass(frozen=True)
class IntakeOutcome:
    """What one intake cycle did, and precisely what it declined to do."""

    opened: tuple[TradeCase, ...] = ()
    refused: tuple[tuple[str, IntakeRefusal], ...] = ()

    @property
    def opened_count(self) -> int:
        return len(self.opened)


class MarketCandidateSource(Protocol):
    """The read intake performs. Recorded observations only, never a provider."""

    async def candidates(
        self, *, include_fixtures: bool = False, limit: int = 50, offset: int = 0
    ) -> tuple[MarketCandidate, ...]: ...

    async def latest(
        self, identity: str, *, include_fixtures: bool = False
    ) -> MarketSnapshot | None: ...


@dataclass(frozen=True)
class CommanderIntakeService:
    """Opens TradeCases from recorded candidates, deterministically and idempotently.

    Concurrency safety does not rest on a check-then-insert. The case identity is
    derived from the candidate, and `open_trade_case` is already idempotent on
    that key at the database level — a unique constraint plus an integrity-error
    replay path — so two workers racing on one candidate converge on one case
    rather than one of them losing.
    """

    cases: TradeCaseService
    markets: MarketCandidateSource
    sessions: async_sessionmaker[AsyncSession]
    policy: CommanderControlPolicy = COMMANDER_CONTROL_V1
    clock: Clock = SystemClock()
    kill_switch: bool = False
    # Supplied only by a deployment that also runs Phase 0 accounting.
    pause: SystemPausePort | None = None

    def intake_key(self, candidate: MarketCandidate) -> str:
        """The deterministic identity of the case this candidate would open.

        Derived from the canonical market identity — never from a ticker, symbol
        or display name, which are not identities and are not unique. Two workers
        handling the same candidate compute the same key and therefore address
        the same case.
        """
        return f"commander-intake:{self.policy.version}:{candidate.pair_id}"

    def intake_correlation(self, candidate: MarketCandidate) -> UUID:
        """The correlation two racing workers must agree on.

        Derived rather than generated, and that is not a detail. `open_trade_case`
        resolves a duplicate insert by comparing the open fingerprint, and the
        correlation is part of it — so a per-worker random value would make two
        workers racing on one candidate produce two different fingerprints and
        collide as a conflict instead of converging on one case. Idempotency that
        only holds when one process runs is not idempotency.
        """
        return uuid5(NAMESPACE_URL, f"rh-agents:{self.intake_key(candidate)}")

    async def run_cycle(self) -> IntakeOutcome:
        """One bounded pass over recorded candidates."""
        if self.kill_switch or await self._paused():
            return IntakeOutcome(refused=(("*", IntakeRefusal.SYSTEM_PAUSED),))

        now = self.clock.now()
        opened: list[TradeCase] = []
        refused: list[tuple[str, IntakeRefusal]] = []
        candidates = await self.markets.candidates(
            include_fixtures=self.policy.allow_fixtures, limit=50
        )
        # Deterministic order, oldest observation first, so a bounded cycle
        # always takes the same candidates from the same set rather than
        # whichever the database happened to return first.
        for candidate in sorted(candidates, key=lambda item: (item.observed_at, item.pair_id)):
            if len(opened) >= self.policy.max_cases_per_cycle:
                refused.append((candidate.pair_id, IntakeRefusal.CYCLE_LIMIT_REACHED))
                continue
            refusal = await self._refusal(candidate, now)
            if refusal is not None:
                refused.append((candidate.pair_id, refusal))
                continue
            case = await self._open(candidate, now)
            if case is None:
                refused.append((candidate.pair_id, IntakeRefusal.MARKET_UNAVAILABLE))
                continue
            opened.append(case)
        return IntakeOutcome(opened=tuple(opened), refused=tuple(refused))

    async def _refusal(self, candidate: MarketCandidate, now: datetime) -> IntakeRefusal | None:
        if candidate.chain not in self.policy.enabled_chains:
            return IntakeRefusal.CHAIN_NOT_ENABLED
        if candidate.is_fixture and not self.policy.allow_fixtures:
            # A synthetic market must never start a real workflow.
            return IntakeRefusal.FIXTURE_MARKET
        # Age is measured from the market's own observation time, never from
        # when we fetched it: a candidate recorded from an hour-old reading
        # describes a market that has moved on.
        if now - candidate.observed_at > self.policy.max_candidate_age:
            return IntakeRefusal.CANDIDATE_TOO_OLD
        if await self._active_case(candidate):
            return IntakeRefusal.ACTIVE_CASE_EXISTS
        return None

    async def _active_case(self, candidate: MarketCandidate) -> bool:
        """Whether this market already has a live case.

        Advisory only: it saves work and produces a clear refusal reason, and it
        is deliberately not the thing that prevents duplicates. The idempotency
        key does that, atomically, where a race can actually be lost.
        """
        async with self.sessions() as session:
            row = await session.scalar(
                select(TradeCaseRow.id)
                .where(
                    TradeCaseRow.market_key == candidate.pair_id,
                    TradeCaseRow.status.notin_([item.value for item in TERMINAL_CASE_STATUSES]),
                )
                .limit(1)
            )
        return row is not None

    async def _open(self, candidate: MarketCandidate, now: datetime) -> TradeCase | None:
        """Open one case from a candidate whose snapshot is still readable.

        The snapshot is re-read rather than reconstructed from the candidate:
        the candidate carries a pair id, and a case needs the full canonical
        identity — base and quote assets, venue, provider — which only the
        recorded market holds. Guessing any of it would be exactly the
        symbol-level identity this system refuses.
        """
        snapshot = await self.markets.latest(
            candidate.pair_id, include_fixtures=self.policy.allow_fixtures
        )
        if snapshot is None:
            return None
        return await self.cases.open_trade_case(
            snapshot.pair.market_identity,
            originating_discovery_reference=candidate.id,
            correlation_id=self.intake_correlation(candidate),
            idempotency_key=self.intake_key(candidate),
            # Measured from the observation that produced this candidate rather
            # than from now, for the same reason as the correlation: two workers
            # reading the same candidate a second apart must compute the same
            # expiry, or their open fingerprints differ and neither wins.
            expires_at=candidate.observed_at + self.policy.case_lifetime,
        )

    async def _paused(self) -> bool:
        """Whether a durable system-wide stop is in force.

        Supplied through the same port the context reader uses, and for the same
        reason: the durable pause lives in the Phase 0 accounting subsystem,
        which the TradeCase workflow has no link to and whose schema a
        workflow-only deployment does not carry.
        """
        return False if self.pause is None else await self.pause.system_paused()
