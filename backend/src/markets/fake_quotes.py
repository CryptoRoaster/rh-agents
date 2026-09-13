"""Deterministic quote sources for tests. Never reachable from production config.

Real execution behaviour is hard to provoke on demand: a market with no route, a
provider that times out mid-ladder, a quote that arrives already stale, a route
that changes shape as the size grows. These sources make each of those a
parameter rather than a wait.

The depth model is deliberately crude and deliberately documented as such. It is
not a simulation of any real automated market maker; it is a monotone curve that
makes larger orders cost more, so that capacity logic can be exercised without
pretending to model a pool.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from src.markets.quotes import (
    ExecutionQuote,
    ExecutionRoute,
    QuoteFailure,
    QuoteUnavailable,
    RouteHop,
    to_human,
)


@dataclass
class FixtureQuoteSource:
    """One configurable market. Every knob corresponds to a real failure mode."""

    reference_price: Decimal
    quoted_at: datetime
    # Basis points of cost added per whole multiple of `depth_notional` traded.
    # A crude monotone stand-in for depth, not a model of one.
    deviation_bps_per_step: Decimal = Decimal(20)
    depth_notional: Decimal = Decimal(1000)
    provider: str = "fixture:quotes"
    # Fail every request with this, for provider-outage and no-route cases.
    always_fails: QuoteFailure | None = None
    # Fail only at or above this notional, for a market that runs out of depth.
    fails_above: Decimal | None = None
    failure_above: QuoteFailure = QuoteFailure.NO_ROUTE
    # Return zero output at or above this notional.
    empty_above: Decimal | None = None
    provider_price_impact_bps: Decimal | None = None
    # Shift each successive quote's timestamp, for ladder-skew cases.
    skew_per_quote: timedelta = timedelta(0)
    # Override the assets a quote claims, for identity-failure cases.
    override_token_out: str | None = None
    override_chain: str | None = None
    # Hops grow with size, as a real aggregator's splits do.
    hops_per_step: int = 1
    max_hops: int = 8
    block_number: int | None = 1_000_000
    calls: list[int] = field(default_factory=list)

    async def quote_exact_input(
        self,
        *,
        chain: str,
        network: str,
        token_in: str,
        token_out: str,
        token_in_decimals: int,
        token_out_decimals: int,
        amount_in: int,
    ) -> ExecutionQuote:
        self.calls.append(amount_in)
        if self.always_fails is not None:
            raise QuoteUnavailable(self.always_fails)
        notional = to_human(amount_in, token_in_decimals)
        if self.fails_above is not None and notional >= self.fails_above:
            raise QuoteUnavailable(self.failure_above)

        steps = notional / self.depth_notional
        effective = self.reference_price * (
            Decimal(1) + (self.deviation_bps_per_step * steps) / Decimal(10000)
        )
        if self.empty_above is not None and notional >= self.empty_above:
            out_units = 0
        else:
            out_units = int(notional / effective * (Decimal(10) ** token_out_decimals))
        hops = min(self.max_hops, max(1, int(steps) * self.hops_per_step or 1))
        quoted_at = self.quoted_at + self.skew_per_quote * len(self.calls)
        out_token = self.override_token_out or token_out
        return ExecutionQuote(
            chain=self.override_chain or chain,
            network=network,
            provider=self.provider,
            token_in=token_in,
            token_out=out_token,
            token_in_decimals=token_in_decimals,
            token_out_decimals=token_out_decimals,
            amount_in=amount_in,
            amount_out=out_units,
            route=ExecutionRoute(
                router="0x" + "ab" * 20,
                # Intermediate legs pass through distinct synthetic tokens, so a
                # multi-hop route never trades a token for itself.
                hops=tuple(
                    RouteHop(
                        venue=f"fixture-venue-{index}",
                        pool="0x" + f"{index:02x}" * 20,
                        token_in=token_in if index == 0 else f"0x{index:039x}1",
                        token_out=(out_token if index == hops - 1 else f"0x{index + 1:039x}1"),
                        amount_in=amount_in if index == 0 else 0,
                        amount_out=out_units if index == hops - 1 else 0,
                    )
                    for index in range(hops)
                ),
            ),
            quoted_at=quoted_at,
            source_block_number=self.block_number,
            provider_price_impact_bps=self.provider_price_impact_bps,
        )
