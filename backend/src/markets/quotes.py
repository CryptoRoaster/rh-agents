"""Executable quotes: what the market will actually give you for a given size.

This is deliberately a different thing from a market observation, and the
distinction is the whole reason the module exists. A snapshot says *the price of
this asset is 215.73*. A quote says *if you put 10,000 units of this exact token
in, on this exact route, you get this exact number of units of that exact token
out*. The first is a fact about a market; the second is an offer, and only the
second can tell you whether a trade of a given size is executable.

Nothing here may be derived from pool liquidity or trading volume. A pool with
ten million dollars of reserves is not an assurance that ten thousand dollars can
be traded through it at an acceptable price, and multiplying a TVL figure by an
arbitrary fraction produces a number with the shape of an answer and none of its
content. Either an authoritative quote exists or the capacity is unknown.

**Amounts are integers at the boundary.** Providers speak in base units, and a
base unit only becomes a human amount through that token's own decimals. Getting
that wrong is not a rounding error: during the research for this phase, sending
an amount computed with the wrong decimals turned a 100 dollar order into a 100
trillion dollar one and made two independent providers look like they were
quoting nonsense. Decimals are therefore required, never assumed, and the
conversion is explicit in both directions.
"""

from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Annotated, Protocol, Self

from pydantic import AwareDatetime, BeforeValidator, Field, model_validator

from src.core.models import Contract
from src.markets.models import Name, Namespace, exact_number

# Base-unit amounts are integers. A token's smallest unit is indivisible, and a
# fractional one would be a quantity the chain cannot represent.
BaseUnits = Annotated[int, Field(strict=True, ge=0, le=2**256 - 1)]
Decimals = Annotated[int, Field(strict=True, ge=0, le=36)]
HumanAmount = Annotated[
    Decimal,
    BeforeValidator(exact_number),
    Field(ge=0, allow_inf_nan=False),
]
Address = Annotated[str, Field(strict=True, pattern=r"^0x[0-9a-f]{40}$")]


def to_human(base_units: int, decimals: int) -> Decimal:
    """Exact conversion from base units, never through a float."""
    if base_units < 0 or decimals < 0:
        raise ValueError("Amounts and decimals are non-negative")
    return Decimal(base_units) / (Decimal(10) ** decimals)


def to_base_units(amount: Decimal, decimals: int) -> int:
    """Exact conversion into base units, refusing anything that would not survive.

    A human amount finer than the token can represent is not rounded down
    quietly: asking for a quantity the chain cannot express means the caller's
    intent and the order would differ, which is exactly the kind of silent
    difference this system refuses elsewhere.
    """
    if amount < 0 or decimals < 0:
        raise ValueError("Amounts and decimals are non-negative")
    scaled = amount * (Decimal(10) ** decimals)
    if scaled != scaled.to_integral_value():
        raise ValueError("Amount is finer than the token's smallest unit")
    return int(scaled)


def percent_to_bps(percent: Decimal) -> Decimal:
    """Normalise a provider's percentage figure into basis points.

    Providers publish price impact in their own units and the unit is rarely
    stated in the response. A value of ``0.04`` from an endpoint documented in
    percent is four basis points, not `0.04` of them, and reading it the wrong
    way understates a cost by a factor of a hundred.

    The sign is preserved rather than normalised here: what a negative means is
    the provider's own semantics, and an adapter that knows its provider decides
    that. Magnitude comparison happens at the policy, so a five percent cost
    expressed as ``-5`` can never pass a three percent bound by being smaller
    than it.
    """
    return percent * Decimal(100)


class QuoteFailure(StrEnum):
    """Why no quote exists, kept apart because they mean different things.

    The distinction that matters most is between *the market cannot do this* and
    *we could not find out*. A pair with no route is a fact about the market and
    is evidence; a provider timing out is an absence of evidence. Collapsing them
    would let an outage look like an illiquid market, which is the reading that
    would stop a trade for the wrong reason — or, worse, the one that would let a
    later retry look like the market having recovered.
    """

    NOT_CONFIGURED = "NOT_CONFIGURED"
    # The provider does not serve this chain — either it never did, or it has
    # stopped. Deliberately not a market fact: a chain the aggregator dropped is
    # a capability we lost, and calling it "no route" would report an empty
    # market for an asset that trades perfectly well.
    UNSUPPORTED_CHAIN = "UNSUPPORTED_CHAIN"
    # Facts about the market.
    NO_ROUTE = "NO_ROUTE"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    # Absences of evidence.
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


# Which failures describe the market rather than our ability to observe it. A
# market fact is usable evidence; an observation failure is not.
MARKET_FACTS = frozenset({QuoteFailure.NO_ROUTE, QuoteFailure.INSUFFICIENT_LIQUIDITY})


class QuoteUnavailable(Exception):
    """No quote could be obtained. Carries a typed reason and nothing else."""

    def __init__(self, failure: QuoteFailure) -> None:
        self.failure = failure
        super().__init__(failure.value)

    @property
    def is_market_fact(self) -> bool:
        return self.failure in MARKET_FACTS


class RouteHop(Contract):
    """One venue in a route, reduced to what a reader needs to understand it.

    Everything a provider includes for its own executor — hook data, pool
    managers, permit addresses, encoded parameters — is dropped at this boundary.
    ANCHOR is not the executor and must not carry the means to become one, and a
    route that travelled with its calldata attached would be one edit away from
    being used.
    """

    venue: Name
    pool: Name
    token_in: Address
    token_out: Address
    amount_in: BaseUnits
    amount_out: BaseUnits


class ExecutionRoute(Contract):
    """How a quote would be filled, typed rather than summarised.

    A split across several venues is not reduced to a single fictional pool. The
    research for this phase found the aggregator spreading a hundred-thousand
    dollar order across seven venues and eight hops while a hundred dollar order
    took one, so pretending a route is always one pool would misdescribe the
    common case rather than the rare one.
    """

    router: Name
    hops: tuple[RouteHop, ...] = Field(min_length=1, max_length=64)

    @property
    def hop_count(self) -> int:
        return len(self.hops)

    @property
    def venues(self) -> frozenset[str]:
        return frozenset(hop.venue for hop in self.hops)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if any(hop.token_in == hop.token_out for hop in self.hops):
            raise ValueError("A hop cannot trade a token for itself")
        return self


class ExecutionQuote(Contract):
    """One provider's offer for one exact input amount, at one moment.

    Carries its own identity and provenance because a quote is only meaningful
    against the market, the assets and the instant it was made for. ``quoted_at``
    is the provider's own account of when it priced this; it is never the moment
    we received the answer, for the same reason source time governs freshness
    everywhere else in this system.
    """

    chain: Namespace
    network: Namespace
    provider: Name
    token_in: Address
    token_out: Address
    token_in_decimals: Decimals
    token_out_decimals: Decimals
    amount_in: BaseUnits = Field(gt=0)
    amount_out: BaseUnits
    route: ExecutionRoute
    # The provider's own account of when it priced this.
    quoted_at: AwareDatetime
    # When the answer arrived here, by the trusted clock. Kept beside the
    # provider's timestamp rather than instead of it, because the meaning of a
    # provider timestamp is rarely documented and a clock we do not control can
    # run ahead of ours. Freshness uses whichever is older, so provider skew can
    # only ever make a quote look staler than it is.
    received_at: AwareDatetime
    # The provider's own USD valuation of the input amount, when it publishes
    # one. A cross-check on our conversion and never the source of it: it
    # arrives with the answer, so it cannot say how much to send.
    provider_amount_in_usd: HumanAmount | None = None
    # Present when the provider states which chain state it priced against.
    # Absent is honest; a fabricated block number would be worse than none.
    source_block_number: int | None = Field(default=None, strict=True, ge=0)
    # The provider's own impact figure, in its own semantics, when it publishes
    # one. Never computed here and never conflated with the deviation ANCHOR
    # derives for itself.
    provider_price_impact_bps: HumanAmount | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.token_in == self.token_out:
            raise ValueError("A quote cannot buy the asset it spends")
        if self.route.hops[0].token_in != self.token_in:
            raise ValueError("A route must begin with the asset being spent")
        if self.route.hops[-1].token_out != self.token_out:
            # The one identity failure that would be most expensive to miss: a
            # route that ends somewhere else buys something else.
            raise ValueError("A route must end with the asset being bought")
        return self

    @property
    def amount_in_human(self) -> Decimal:
        return to_human(self.amount_in, self.token_in_decimals)

    @property
    def amount_out_human(self) -> Decimal:
        return to_human(self.amount_out, self.token_out_decimals)

    def effective_price(self) -> Decimal | None:
        """What one output unit actually costs, in input units.

        ``None`` when the quote buys nothing, because a price per zero units is
        not a very large number — it is not a number.
        """
        out = self.amount_out_human
        if out == 0:
            return None
        with localcontext() as context:
            # Enough working precision that the division is exact before the
            # caller decides what scale to record it at.
            context.prec = 60
            return self.amount_in_human / out

    @property
    def priced_at(self) -> datetime:
        """The time freshness is judged from: the earlier of the two we hold.

        A provider clock running ahead of ours would otherwise make a stale
        quote look fresh, which is the one direction of error that costs money.
        """
        return min(self.quoted_at, self.received_at)

    def age(self, now: datetime) -> timedelta:
        if now.utcoffset() is None:
            raise ValueError("Quote freshness requires timezone-aware time")
        return now - self.priced_at


class ExecutionQuoteSource(Protocol):
    """The one read an execution assessment performs.

    Deliberately not a general client. There is no method here to fetch a URL,
    make an RPC call, or send calldata anywhere — a port that could do those
    things would make every consumer of it one line away from being an executor.
    """

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
    ) -> ExecutionQuote: ...


class UnconfiguredQuoteSource:
    """The default. No quote provider is wired, and that is said rather than implied.

    Returning "no route" here would let an unconfigured deployment look like a
    market with no liquidity, and the two call for opposite responses: one is
    fixed by configuration, the other by not trading.
    """

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
        raise QuoteUnavailable(QuoteFailure.NOT_CONFIGURED)
