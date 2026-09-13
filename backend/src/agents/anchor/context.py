"""Assembly of the ANCHOR view: the triggered setup, a reference, and a ladder.

No model participates. The reader binds the exact setup and trigger the workflow
currently considers authoritative, reads one independent reference price, and
walks the policy's ladder asking the quote source for one exact-input quote per
size — stopping at the first size the market will not serve, because capacity is
monotone in intent and continuing would spend requests to learn nothing.

Two decisions shape this file.

**The ladder stops early, and the reason it stopped is typed.** A provider that
says "no route" has told us something about the market; a provider that times
out has told us nothing. Both end the ladder, and they must not arrive at the
assessment looking the same, so the distinction is carried rather than flattened.

**The payment asset is the market's own quote asset.** Not a wrapped native
token chosen by convention, not a stablecoin assumed to be present — the exact
asset the recorded market is denominated in, with its own authoritative decimals.
Guessing either would produce quotes for a trade nobody intends to make.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from src.agents.anchor.models import (
    AnchorMarketContext,
    AnchorTaskInput,
    QuoteAttempt,
    ReferenceMarket,
)
from src.agents.anchor.policy import ANCHOR_EXECUTION_V1, AnchorExecutionPolicy
from src.agents.anchor.ports import AnchorContextUnavailable
from src.core.clock import Clock, SystemClock
from src.markets.models import Availability, MarketSnapshot
from src.markets.quotes import (
    ExecutionQuoteSource,
    QuoteUnavailable,
    UnconfiguredQuoteSource,
    to_base_units,
)
from src.orchestration.workflow.engine import active_evidence, unusable_reason
from src.orchestration.workflow.models import (
    EvidenceEnvelope,
    EvidenceType,
    TradeCase,
    TradeSetupPayload,
    TriggerPayload,
)


class TradeCaseExecutionSource(Protocol):
    async def get_trade_case(self, trade_case_id: UUID) -> TradeCase: ...

    async def evidence(self, trade_case_id: UUID) -> tuple[EvidenceEnvelope, ...]: ...


class AnchorMarketInput(Protocol):
    """Recorded market data only: no DB writes, no provider or transport client."""

    async def latest(
        self, identity: str, *, include_fixtures: bool = False
    ) -> MarketSnapshot | None: ...


def token_address(asset_id: str) -> str:
    """The bare address out of a chain-scoped asset id, lowercased."""
    return asset_id.rsplit(":", 1)[-1].lower()


def market_context(snapshot: MarketSnapshot, trade_case: TradeCase) -> AnchorMarketContext:
    """Bind the exact assets a quote must name, with authoritative decimals.

    Decimals come from the recorded pair rather than from a convention. Assuming
    eighteen is the mistake that turns a hundred-dollar order into a hundred
    trillion one, and a provider will answer that question rather than refuse it.
    """
    base, quote = snapshot.pair.base, snapshot.pair.quote
    if base.decimals is None or quote.decimals is None:
        raise AnchorContextUnavailable("TOKEN_DECIMALS_UNKNOWN")
    return AnchorMarketContext(
        pair_id=snapshot.pair.pair_id,
        chain=snapshot.chain,
        network=snapshot.network,
        venue=snapshot.pair.venue,
        base_asset_id=trade_case.market.base_asset_id,
        quote_asset_id=trade_case.market.quote_asset_id,
        base_token=token_address(trade_case.market.base_asset_id),
        quote_token=token_address(trade_case.market.quote_asset_id),
        base_decimals=base.decimals,
        quote_decimals=quote.decimals,
    )


def reference_market(snapshot: MarketSnapshot, now: datetime) -> ReferenceMarket | None:
    """The independent current price, or nothing when the market has no usable one."""
    if snapshot.price.status != Availability.AVAILABLE or snapshot.price.value_usd is None:
        return None
    if snapshot.price.value_usd <= 0:
        return None
    return ReferenceMarket(
        snapshot_id=snapshot.id,
        observation_id=snapshot.price.id,
        pair_id=snapshot.pair.pair_id,
        chain=snapshot.chain,
        network=snapshot.network,
        provider=snapshot.provider,
        price=snapshot.price.value_usd,
        price_basis="USD_PER_BASE_UNIT",
        liquidity_usd=(
            snapshot.liquidity.value_usd
            if snapshot.liquidity.status == Availability.AVAILABLE
            else None
        ),
        observed_at=snapshot.price.observed_at,
        age_seconds=max(0, int((now - snapshot.price.observed_at).total_seconds())),
    )


@dataclass(frozen=True)
class AnchorContextReader:
    """Assembles the execution view from existing workflow, market and quote services."""

    cases: TradeCaseExecutionSource
    markets: AnchorMarketInput
    # Defaults to the source that says no provider is wired, so a deployment
    # that forgot to configure one refuses rather than reporting an empty market.
    quotes: ExecutionQuoteSource = UnconfiguredQuoteSource()
    policy: AnchorExecutionPolicy = ANCHOR_EXECUTION_V1
    clock: Clock = SystemClock()
    include_fixtures: bool = False

    async def execution_context(self, trade_case_id: UUID, task_id: UUID) -> AnchorTaskInput:
        trade_case = await self.cases.get_trade_case(trade_case_id)
        now = self.clock.now()
        current = active_evidence(await self.cases.evidence(trade_case_id))

        setup = current.get(EvidenceType.TRADE_SETUP)
        trigger = current.get(EvidenceType.TRIGGER)
        if setup is None or trigger is None:
            # ANCHOR runs after a trigger, never instead of one.
            raise AnchorContextUnavailable("NO_TRIGGERED_SETUP")
        if unusable_reason(setup, now) is not None or unusable_reason(trigger, now) is not None:
            raise AnchorContextUnavailable("NO_TRIGGERED_SETUP")
        if not isinstance(setup.payload, TradeSetupPayload) or setup.payload.setup is None:
            raise AnchorContextUnavailable("NO_TRIGGERED_SETUP")
        if not isinstance(trigger.payload, TriggerPayload):
            raise AnchorContextUnavailable("NO_TRIGGERED_SETUP")
        if trigger.payload.setup_evidence_id != setup.evidence_id:
            # The trigger belongs to a setup that is no longer current.
            raise AnchorContextUnavailable("TRIGGER_NOT_FOR_CURRENT_SETUP")

        snapshot = await self.markets.latest(
            trade_case.market.pair_id, include_fixtures=self.include_fixtures
        )
        if snapshot is None:
            raise AnchorContextUnavailable("MARKET_OBSERVATION_MISSING")
        if snapshot.pair.pair_id != trade_case.market.pair_id:
            raise AnchorContextUnavailable("MARKET_IDENTITY_MISMATCH")

        market = market_context(snapshot, trade_case)
        reference = reference_market(snapshot, now)
        ladder, requests = await self._ladder(market)
        return AnchorTaskInput(
            trade_case_id=trade_case_id,
            task_id=task_id,
            setup_evidence_id=setup.evidence_id,
            setup_id=setup.payload.setup_id,
            setup_fingerprint=setup.payload.setup.setup_fingerprint,
            trigger_evidence_id=trigger.evidence_id,
            market=market,
            reference=reference,
            ladder=ladder,
            quote_requests=requests,
            policy_version=self.policy.version,
            evaluated_at=now,
        )

    async def _ladder(self, market: AnchorMarketContext) -> tuple[tuple[QuoteAttempt, ...], int]:
        """Walk the policy's sizes, stopping at the first the market will not serve.

        Bounded by construction rather than by a runtime check. The ladder is a
        fixed list of at most twelve sizes and the policy refuses to exist unless
        its stated request budget covers that list, so the number of provider
        calls one assessment can make is decided when the policy is written. A
        guard inside this loop would be a branch that could never fire, which is
        not a second safeguard but an untestable one.

        There is no search and no refinement here: walk the sizes, stop at the
        first the market will not serve.
        """
        attempts: list[QuoteAttempt] = []
        requests = 0
        for notional in self.policy.ladder_notional:
            amount_in = to_base_units(notional, market.quote_decimals)
            requests += 1
            try:
                quote = await self.quotes.quote_exact_input(
                    chain=market.chain,
                    network=market.network,
                    token_in=market.quote_token,
                    token_out=market.base_token,
                    token_in_decimals=market.quote_decimals,
                    token_out_decimals=market.base_decimals,
                    amount_in=amount_in,
                )
            except QuoteUnavailable as error:
                attempts.append(
                    QuoteAttempt(notional=notional, amount_in=amount_in, failure=error.failure)
                )
                # Whether this ends the ladder as a market fact or as an absence
                # of evidence is the assessment's decision, not this one's.
                break
            attempts.append(QuoteAttempt(notional=notional, amount_in=amount_in, quote=quote))
        return tuple(attempts), requests
