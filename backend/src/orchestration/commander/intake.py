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

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.clock import Clock, SystemClock
from src.data.tables import TradeCaseRow
from src.markets.models import MarketCandidate, MarketSnapshot
from src.orchestration.commander.context import SystemPausePort
from src.orchestration.commander.policy import COMMANDER_CONTROL_V1, CommanderControlPolicy
from src.orchestration.workflow.models import (
    MARKET_BARRING_CASE_STATUSES,
    TERMINAL_CASE_STATUSES,
    TradeCase,
    TradeCaseStatus,
    WorkflowFailure,
)
from src.orchestration.workflow.service import TradeCaseService

logger = logging.getLogger(__name__)


async def active_case_exists(session: AsyncSession, pair_id: str) -> bool:
    """Whether any live case exists for this market. See `_active_case`.

    Module-level so a candidate source can ask COMMANDER's exact question
    before its limit is applied, rather than restating the rule.
    """
    row = await session.scalar(
        select(TradeCaseRow.id)
        .where(
            TradeCaseRow.market_key == pair_id,
            TradeCaseRow.status.notin_([item.value for item in TERMINAL_CASE_STATUSES]),
        )
        .limit(1)
    )
    return row is not None


async def market_barring(session: AsyncSession, pair_id: str) -> TradeCaseStatus | None:
    """Which case, if any, has spoken for this market. See `_barred`.

    `EXECUTED` wins over `RISK_REJECTED` when both appear, because it is the one
    that left a position and the one whose successor has a contract.
    """
    rows = (
        await session.scalars(
            select(TradeCaseRow.status).where(
                TradeCaseRow.market_key == pair_id,
                TradeCaseRow.status.in_([item.value for item in MARKET_BARRING_CASE_STATUSES]),
            )
        )
    ).all()
    found = {TradeCaseStatus(item) for item in rows}
    if TradeCaseStatus.EXECUTED in found:
        return TradeCaseStatus.EXECUTED
    if TradeCaseStatus.RISK_REJECTED in found:
        return TradeCaseStatus.RISK_REJECTED
    return None


class SystemPaused(Exception):
    """A stop was in force when the opening transaction reached it."""


class IntakeRefusal(StrEnum):
    """Why a candidate did not open a case. Typed, so the answer is auditable."""

    CHAIN_NOT_ENABLED = "CHAIN_NOT_ENABLED"
    FIXTURE_MARKET = "FIXTURE_MARKET"
    CANDIDATE_TOO_OLD = "CANDIDATE_TOO_OLD"
    MARKET_UNAVAILABLE = "MARKET_UNAVAILABLE"
    IDENTITY_INVALID = "IDENTITY_INVALID"
    ACTIVE_CASE_EXISTS = "ACTIVE_CASE_EXISTS"
    # This exact intake generation has already been opened. A replay, not a new
    # case — and reporting it as opened is what made a cancelled predecessor
    # look like fresh work.
    ALREADY_OPENED = "ALREADY_OPENED"
    # SENTINEL rejected the most recent case for this market. Re-observing a
    # market is not new information about risk, and must not launder a refusal.
    RISK_REJECTED_FOR_MARKET = "RISK_REJECTED_FOR_MARKET"
    # The most recent case for this market was filled, so a position exists.
    # Adding to it, exiting it, or deciding that a further entry is a different
    # trade are all contracts this system does not have, and intake must not
    # invent one by simply observing the market again.
    POSITION_OPENED_FOR_MARKET = "POSITION_OPENED_FOR_MARKET"
    CYCLE_LIMIT_REACHED = "CYCLE_LIMIT_REACHED"
    SYSTEM_PAUSED = "SYSTEM_PAUSED"
    # The open was refused by the workflow itself. Recorded per candidate so one
    # bad candidate cannot abort a whole cycle.
    OPEN_REFUSED = "OPEN_REFUSED"


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

    def intake_key(self, candidate: MarketCandidate, predecessor: UUID | None) -> str:
        """The deterministic identity of the case this candidate would open.

        Three requirements pull in different directions and this is the smallest
        shape that satisfies all of them.

        *Replay must converge.* Two workers handling the same candidate, and the
        same worker retrying, must address one case — so the key cannot contain
        anything per-worker or per-observation.

        *One active case per market.* So the key cannot be unique per candidate
        either; that would open a case per observation.

        *A terminal predecessor must not block the market forever.* A key that is
        constant per market does exactly that: once the first case ends, every
        later observation either replays the dead case or collides with it.

        So the key is scoped to the **generation**: the market, plus the case
        that last ended for it. Everyone computing it sees the same predecessor
        and derives the same key, and the key changes exactly once per
        generation — never per observation and never per worker.

        The market identity is canonical throughout. Never a ticker, symbol or
        display name, which are not identities and are not unique.
        """
        generation = "initial" if predecessor is None else str(predecessor)
        return f"commander-intake:{self.policy.version}:{candidate.pair_id}:{generation}"

    def intake_correlation(self, candidate: MarketCandidate, predecessor: UUID | None) -> UUID:
        """The correlation two racing workers must agree on.

        Derived rather than generated, and that is not a detail. `open_trade_case`
        resolves a duplicate insert by comparing the open fingerprint, and the
        correlation is part of it — so a per-worker random value would make two
        workers racing on one candidate produce two different fingerprints and
        collide as a conflict instead of converging on one case. Idempotency that
        only holds when one process runs is not idempotency.
        """
        return uuid5(NAMESPACE_URL, f"rh-agents:{self.intake_key(candidate, predecessor)}")

    async def run_cycle(self, *, limit: int = 50) -> IntakeOutcome:
        """One bounded pass over recorded candidates.

        `limit` bounds how many candidates are *read and judged*, which is a
        different quantity from how many may become cases — that one is
        `policy.max_cases_per_cycle`. A caller working to its own budget says
        how much of the recorded market this pass should look at; how much of it
        may be taken on stays a policy decision.
        """
        if not 1 <= limit <= 50:
            raise ValueError("One intake cycle must read between one and fifty candidates")
        if await self._halted():
            return IntakeOutcome(refused=(("*", IntakeRefusal.SYSTEM_PAUSED),))

        now = self.clock.now()
        opened: list[TradeCase] = []
        refused: list[tuple[str, IntakeRefusal]] = []
        candidates = await self.markets.candidates(
            include_fixtures=self.policy.allow_fixtures, limit=limit
        )
        # Deterministic order, oldest observation first, so a bounded cycle
        # always takes the same candidates from the same set rather than
        # whichever the database happened to return first.
        for candidate in sorted(candidates, key=lambda item: (item.observed_at, item.pair_id)):
            if len(opened) >= self.policy.max_cases_per_cycle:
                refused.append((candidate.pair_id, IntakeRefusal.CYCLE_LIMIT_REACHED))
                continue
            refusal, predecessor = await self._refusal(candidate, now)
            if refusal is not None:
                refused.append((candidate.pair_id, refusal))
                continue
            try:
                case, created = await self._open(candidate, now, predecessor)
            except SystemPaused:
                # The stop was in force when the transaction reached it.
                refused.append((candidate.pair_id, IntakeRefusal.SYSTEM_PAUSED))
                continue
            except WorkflowFailure:
                # One candidate the workflow declined must not end the cycle.
                # An earlier implementation let an idempotency conflict escape
                # and take every remaining candidate down with it.
                refused.append((candidate.pair_id, IntakeRefusal.OPEN_REFUSED))
                continue
            if case is None:
                refused.append((candidate.pair_id, IntakeRefusal.MARKET_UNAVAILABLE))
                continue
            if not created:
                # Another worker created it while this one was still preparing.
                # Reporting it as opened made two workers claim one case between
                # them and spend two units of a budget that bounds *new* cases.
                refused.append((candidate.pair_id, IntakeRefusal.ALREADY_OPENED))
                continue
            opened.append(case)
        return IntakeOutcome(opened=tuple(opened), refused=tuple(refused))

    async def _active_case(self, candidate: MarketCandidate) -> bool:
        """Whether any live case exists for this market.

        Deliberately a question about *any* non-terminal case rather than about
        the newest one. The newest-row ordering is only a total order, not a
        statement about which case is alive, and two cases opened in the same
        instant tie — so asking "is the newest one active" would let an active
        case hide behind a terminal sibling. The safety rule is "at most one
        active case per market", so that is the question asked.
        """
        async with self.sessions() as session:
            return await active_case_exists(session, candidate.pair_id)

    async def _barred(self, candidate: MarketCandidate) -> TradeCaseStatus | None:
        """Whether *any* case for this market has spoken for it.

        Deliberately a question about every case rather than about the newest
        terminal one, for the same reason `_active_case` asks about every live
        case: the newest-row ordering is a total order, not a statement about
        what a market has already done. An executed cycle that was later
        followed by a case which expired or was cancelled would otherwise look
        unbarred, and intake would open a generation on a market whose one
        legitimate successor path is the explicit re-entry contract.

        `EXECUTED` wins over `RISK_REJECTED` when both appear, because it is the
        one that left a position and the one whose successor has a contract.
        """
        async with self.sessions() as session:
            return await market_barring(session, candidate.pair_id)

    async def _latest_terminal(
        self, candidate: MarketCandidate
    ) -> tuple[UUID | None, TradeCaseStatus | None]:
        """The most recently ended case for this market: the generation predecessor.

        Only consulted once no live case exists. Ordered by open time with the
        identifier as a stable tiebreak, so every worker derives the same
        generation from the same rows — a count would instead depend on which
        rows a racing transaction could already see.
        """
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(TradeCaseRow.id, TradeCaseRow.status)
                    .where(
                        TradeCaseRow.market_key == candidate.pair_id,
                        TradeCaseRow.status.in_([item.value for item in TERMINAL_CASE_STATUSES]),
                    )
                    .order_by(TradeCaseRow.opened_at.desc(), TradeCaseRow.id.desc())
                    .limit(1)
                )
            ).first()
        if row is None:
            return None, None
        return row[0], TradeCaseStatus(row[1])

    async def _already_opened(self, key: str) -> bool:
        """Whether this exact generation has been opened before."""
        async with self.sessions() as session:
            existing = await session.scalar(
                select(TradeCaseRow.id).where(TradeCaseRow.open_idempotency_key == key).limit(1)
            )
        return existing is not None

    async def _refusal(
        self, candidate: MarketCandidate, now: datetime
    ) -> tuple[IntakeRefusal | None, UUID | None]:
        """Whether this candidate may open a case, and which generation it is.

        Operational checks only. Nothing here weighs the market.
        """
        if candidate.chain not in self.policy.enabled_chains:
            return IntakeRefusal.CHAIN_NOT_ENABLED, None
        if candidate.is_fixture and not self.policy.allow_fixtures:
            # A synthetic market must never start a real workflow.
            return IntakeRefusal.FIXTURE_MARKET, None
        # Age is measured from the market's own observation time, never from
        # when we fetched it: a candidate recorded from an hour-old reading
        # describes a market that has moved on.
        if now - candidate.observed_at > self.policy.max_candidate_age:
            return IntakeRefusal.CANDIDATE_TOO_OLD, None

        if await self._active_case(candidate):
            return IntakeRefusal.ACTIVE_CASE_EXISTS, None

        barring = await self._barred(candidate)
        if barring is not None:
            # One set decides whether a market is spoken for; the codes differ
            # because the reasons do. A rejection is a verdict about the market,
            # and re-observing it is not new information about risk. An
            # execution left a position, and adding to one, exiting one or
            # deciding that a further entry is a different trade are contracts
            # this system does not have — intake must not invent one by looking
            # at the market again. Re-entry after a completed cycle exists, and
            # goes through its own explicit contract rather than through here.
            return (
                IntakeRefusal.POSITION_OPENED_FOR_MARKET
                if barring is TradeCaseStatus.EXECUTED
                else IntakeRefusal.RISK_REJECTED_FOR_MARKET
            ), None

        latest_id, _ = await self._latest_terminal(candidate)

        # EXPIRED and CANCELLED end a case without deciding anything about the
        # market and without leaving a position behind, so the next observation
        # may open the next generation.
        key = self.intake_key(candidate, latest_id)
        if await self._already_opened(key):
            return IntakeRefusal.ALREADY_OPENED, latest_id
        return None, latest_id

    async def _open(
        self, candidate: MarketCandidate, now: datetime, predecessor: UUID | None
    ) -> tuple[TradeCase | None, bool]:
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
            return None, False

        # One transaction, in one order: take the pause lock, then open.
        #
        # Checking the stop beforehand cannot be made safe by checking it later,
        # however late. Between any read and the insert there is a window, and a
        # pause committed inside it is one this cycle has already passed. Holding
        # the writer's own lock across the insert removes the window rather than
        # narrowing it: whichever transaction takes the lock first wins, and the
        # loser observes the winner's committed state.
        #
        # Lock order is paper account, then trade case. See `locked_paused`.
        async with self.sessions.begin() as session:
            if self.kill_switch or self.pause is None:
                raise SystemPaused
            if await self.pause.locked_paused(session):
                raise SystemPaused
            await self._serialize_on_key(session, self.intake_key(candidate, predecessor))
            return await self.cases.open_trade_case_in_session(
                session,
                snapshot.pair.market_identity,
                originating_discovery_reference=candidate.id,
                correlation_id=self.intake_correlation(candidate, predecessor),
                idempotency_key=self.intake_key(candidate, predecessor),
                # Measured from the observation that produced this candidate
                # rather than from now, for the same reason as the correlation:
                # two workers reading the same candidate a second apart must
                # compute the same expiry, or their open fingerprints differ and
                # neither wins.
                expires_at=candidate.observed_at + self.policy.case_lifetime,
            )

    @staticmethod
    async def _serialize_on_key(session: AsyncSession, key: str) -> None:
        """Hold a transaction-scoped lock on this exact intake generation.

        Deliberately not a reliance on the pause lock. That one happens to
        serialise intake as a side effect, but only when a real account row is
        being locked — so the guarantee would quietly disappear wherever the
        pause source were stubbed or absent, which is precisely how an injected
        boolean port can look like a concurrency solution without being one.

        Locking the key itself is the intent: two transactions opening the same
        generation queue, and the second observes the first's committed row and
        reports a replay instead of colliding on the primary key. A conflict
        inside a caller-owned transaction cannot be recovered from, because the
        transaction is already poisoned by the time it surfaces.

        PostgreSQL is the authoritative database; SQLite has no advisory lock and
        the light test suite runs no concurrent intake.
        """
        if session.bind is None or session.bind.dialect.name != "postgresql":
            return
        await session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})

    async def _halted(self) -> bool:
        """Whether any system-wide stop is in force.

        Fails closed on every uncertainty. A missing pause port, a missing
        account row or a control that cannot be read are all *unknown*, and
        unknown is not permission — this system's standing rule everywhere else.
        The previous implementation treated all three as "not paused", which
        turned an unavailable stop into a green light.
        """
        if self.kill_switch:
            return True
        if self.pause is None:
            # No stop source configured. A service that may open real cases must
            # be told where the stop lives; not knowing is not the same as being
            # told there is none.
            return True
        return await self.pause.system_paused()
