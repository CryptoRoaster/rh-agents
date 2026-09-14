"""The narrow read surface a sizing assessment is built on.

Two ports, both read-only, neither general. There is no session here, no
repository, no provider client, no HTTP or RPC transport and no way to ask a
different question — the reader is handed the case source and the recorded
market layer, and that is the whole of what sizing may look at.

That narrowness is the point rather than tidiness. Sizing decides how much money
a request asks for; a component holding a database handle or a quote client
would be one line away from also deciding when, and from acting on it.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from src.core.clock import Clock, SystemClock
from src.core.models import TradingMode
from src.markets.models import Availability, MarketSnapshot
from src.orchestration.sizing.calculator import assess_paper_sizing
from src.orchestration.sizing.models import (
    BaseAssetMetadata,
    ReferencePrice,
    SizingPolicySnapshot,
    SizingReading,
    SizingRefusal,
    SizingRefused,
)
from src.orchestration.sizing.policy import PAPER_SIZING_V1, PaperSizingPolicy
from src.orchestration.workflow.engine import active_evidence
from src.orchestration.workflow.models import (
    EvidenceEnvelope,
    EvidenceType,
    TradeCase,
    TradeSetupPayload,
    WorkflowFailure,
)


class SizingCaseSource(Protocol):
    """Read-only access to one trade case and its evidence."""

    async def get_trade_case(self, trade_case_id: UUID) -> TradeCase: ...

    async def evidence(self, trade_case_id: UUID) -> tuple[EvidenceEnvelope, ...]: ...


class SizingMarketInput(Protocol):
    """Recorded market observations only: no writes, no provider, no transport."""

    async def latest(
        self, identity: str, *, include_fixtures: bool = False
    ) -> MarketSnapshot | None: ...


@dataclass(frozen=True)
class PaperSizingReader:
    """Assembles the sizing inputs for one case and answers with a typed reading.

    Nothing is produced here beyond that reading. No evidence is written, no
    task is completed, no status is touched, and no `TradeIntent` is built —
    that object is what would carry a size into risk evaluation, and building
    one is a later phase with its own durable identity to settle first.
    """

    cases: SizingCaseSource
    markets: SizingMarketInput
    # The operator's configured amount. `None` is the default and the honest
    # one: an unset knob is not a reason to choose a number.
    requested_notional_usd: Decimal | None = None
    policy: PaperSizingPolicy = PAPER_SIZING_V1
    # Fails closed. A deployment that has not said it is in PAPER has not said
    # it may size anything, and the default must never be the permissive value.
    trading_mode: TradingMode = TradingMode.OBSERVE
    clock: Clock = SystemClock()
    include_fixtures: bool = False

    async def sizing(self, trade_case_id: UUID) -> SizingReading:
        try:
            trade_case = await self.cases.get_trade_case(trade_case_id)
        except WorkflowFailure:
            return self._refused(trade_case_id, None, SizingRefusal.SIZING_CASE_UNAVAILABLE)

        base_asset_id = trade_case.market.base_asset_id
        try:
            current = active_evidence(await self.cases.evidence(trade_case_id))
        except WorkflowFailure:
            # Inconsistent stored history. Not something sizing resolves.
            return self._refused(
                trade_case_id, base_asset_id, SizingRefusal.SIZING_CASE_UNAVAILABLE
            )

        setup = current.get(EvidenceType.TRADE_SETUP)
        if setup is None or not isinstance(setup.payload, TradeSetupPayload):
            return self._refused(
                trade_case_id, base_asset_id, SizingRefusal.SIZING_NO_CURRENT_SETUP
            )

        snapshot = await self.markets.latest(
            trade_case.market.pair_id, include_fixtures=self.include_fixtures
        )
        if snapshot is None:
            return self._refused(
                trade_case_id, base_asset_id, SizingRefusal.SIZING_MARKET_NOT_RECORDED
            )

        # The instant is read *after* every await, never before them.
        #
        # Reading it first measured freshness at the moment the work started
        # rather than at the moment it finished, so an eighty-nine-second-old
        # price stayed acceptable across a database read and a market read that
        # together took two seconds — and the assessment came back already past
        # its own `valid_until`. Nothing here extends a deadline; the deadline
        # is simply compared against the time it is actually being used at.
        now = self.clock.now()
        return assess_paper_sizing(
            trade_case_id=trade_case_id,
            base_asset_id=base_asset_id,
            setup_evidence_id=setup.evidence_id,
            # Only the side is read from the setup, and deliberately only the
            # side. A setup's entry price is where somebody proposes to act, not
            # an independent valuation of the token, and sizing against it would
            # value the position at the price the proposal wanted to see.
            side=setup.payload.side,
            trading_mode=self.trading_mode,
            requested_notional_usd=self.requested_notional_usd,
            price=reference_price(snapshot),
            base_asset=base_asset_metadata(snapshot),
            now=now,
            policy=self.policy,
        )

    def _refused(
        self, trade_case_id: UUID, base_asset_id: str | None, reason: SizingRefusal
    ) -> SizingRefused:
        return SizingRefused(
            reason=reason,
            policy=SizingPolicySnapshot.of(self.policy),
            trade_case_id=trade_case_id,
            base_asset_id=base_asset_id,
        )


def reference_price(snapshot: MarketSnapshot) -> ReferencePrice | None:
    """The recorded USD price of the snapshot's own base asset, if it has one.

    A market snapshot prices the asset it identifies — the model enforces that
    `asset_id` equals the pair's base asset, and that nested observations carry
    the same provenance — so `price` here is unambiguously USD per base unit
    rather than a pair rate that would need inverting.

    Never a stablecoin assumption. A quote asset believed to be worth a dollar
    is worth a dollar until the day it is not, and that day is exactly when a
    size derived from the assumption would be most wrong.
    """
    price = snapshot.price
    if price.status != Availability.AVAILABLE or price.value_usd is None or price.value_usd <= 0:
        return None
    return ReferencePrice(
        snapshot_id=snapshot.id,
        observation_id=price.id,
        provider=price.provider,
        asset_id=price.asset_id,
        usd_per_base_unit=price.value_usd,
        observed_at=price.observed_at,
    )


def base_asset_metadata(snapshot: MarketSnapshot) -> BaseAssetMetadata | None:
    """The base token's decimals as recorded, or nothing when they are unknown.

    This is the one trusted metadata source that exists today: the decimals the
    market provider published when it observed the pair, which is the same
    figure ANCHOR binds its quotes to. ATLAS does read ERC-20 `decimals()` on
    chain, but that value never reaches durable evidence — it enters a digest
    and is gone — so it cannot be read back here, and inventing a second
    provider integration to reach it is not this phase's work.

    Absent decimals produce nothing at all. Assuming eighteen is the mistake
    that turns a hundred-dollar order into a hundred-trillion one on a
    six-decimal token, and the honest answer to "how many decimals?" when
    nothing recorded it is that we do not know.
    """
    base = snapshot.pair.base
    if base.decimals is None:
        return None
    return BaseAssetMetadata(
        asset_id=base.asset_id,
        symbol=base.symbol,
        decimals=base.decimals,
        source_provider=base.provider,
        source_observation_id=base.id,
        source_observed_at=base.observed_at,
    )
