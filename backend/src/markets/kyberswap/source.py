"""KyberSwap aggregator quotes, verified against both supported chains.

Chosen after comparing the current public documentation and live behaviour of
several aggregators. What decided it:

* **Both chains are genuinely supported.** Robinhood Chain (4663) and BNB Smart
  Chain (56) both return routes. That was checked with real tokens rather than
  assumed from a claim of generic EVM support — and it matters, because most
  candidates cover BSC and not Robinhood.
* **The quote endpoint returns no calldata.** `GET /routes` answers with amounts
  and a route; building a transaction is a separate `POST /route/build` this
  system never calls. It does return a `routerAddress` — the contract a swap
  would be sent to — which is why this adapter does not parse that field at all.
  Nothing here can be turned into an executor by accident.
* **No credential.** The public tier needs no key, so there is no secret to leak
  and no cost to a read.

Verified during this phase against the live API: a hundred dollars of USDG into
NVDA on Robinhood quoted two basis points from the recorded reference, and a
million dollars forty-seven, through a route that grew from one hop to eight
across seven venues. Those figures are what the deviation bound was calibrated
against.

One correction from that research is worth recording. An early probe appeared to
show the provider quoting nonsense — a hundred dollars buying five million
dollars of stock. The provider was right and the probe was wrong: the payment
asset has six decimals, not eighteen, so the request had actually asked for a
hundred trillion dollars. Decimals are supplied explicitly here and never
inferred.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from types import TracebackType
from typing import Annotated

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.core.clock import Clock, SystemClock
from src.core.config import Settings
from src.markets.quotes import (
    ExecutionQuote,
    ExecutionRoute,
    QuoteFailure,
    QuoteUnavailable,
    RouteHop,
)

# The provider's own chain slugs. Only the chains this system supports appear
# here: an unlisted chain is refused rather than guessed at from a chain id.
CHAIN_SLUGS: dict[str, str] = {"robinhood": "robinhood", "bsc": "bsc"}

Text = Annotated[str, Field(strict=True, min_length=1, max_length=200)]
Address = Annotated[str, Field(strict=True, pattern=r"^0x[0-9a-fA-F]{40}$")]


class DTO(BaseModel):
    # Additive provider fields are ignored rather than rejected, but nothing
    # undeclared travels onward either. In particular the provider's `extra` and
    # `poolExtra` blobs — hook data, pool managers, permit addresses — are simply
    # never read, which is how execution machinery stays out of this system.
    model_config = ConfigDict(extra="ignore", frozen=True, hide_input_in_errors=True)


class Hop(DTO):
    pool: Text
    exchange: Text
    tokenIn: Address  # noqa: N815 - provider field name
    tokenOut: Address  # noqa: N815 - provider field name
    swapAmount: Text  # noqa: N815 - provider field name
    amountOut: Text  # noqa: N815 - provider field name


class RouteSummary(DTO):
    tokenIn: Address  # noqa: N815 - provider field name
    tokenOut: Address  # noqa: N815 - provider field name
    amountIn: Text  # noqa: N815 - provider field name
    amountOut: Text  # noqa: N815 - provider field name
    route: list[list[Hop]] = Field(max_length=64)
    timestamp: int = Field(strict=True, gt=0)
    # The provider's own USD valuation of the input. Read as a cross-check on our
    # conversion, never as its source: it arrives with the answer.
    amountInUsd: Decimal | None = None  # noqa: N815 - provider field name


class RouteData(DTO):
    # `routerAddress` is deliberately absent. It is the contract a swap would be
    # sent *to* — one of the three fields that must never reach an assessment —
    # and nothing downstream reads it, so parsing it would create the liability
    # and buy nothing. A future EXECUTOR obtains its own from its own build call.
    routeSummary: RouteSummary  # noqa: N815 - provider field name


class RouteResponse(DTO):
    code: int = Field(strict=True)
    message: Text | None = None
    data: RouteData | None = None


# Provider response codes that describe the market rather than a failure to
# answer. Anything else is an absence of evidence.
NO_ROUTE_CODES = frozenset({4005, 4008, 4011})


def _usd(raw: Decimal | None) -> Decimal | None:
    """The provider's USD valuation, or nothing. Never a reason to fail a quote.

    A cross-check that cannot be read is simply absent; refusing an otherwise
    valid quote because its optional annotation was malformed would turn a
    convenience into a liability.
    """
    if raw is None or not raw.is_finite() or raw < 0:
        return None
    return raw


def _amount(raw: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise QuoteUnavailable(QuoteFailure.INVALID_RESPONSE) from None
    if value < 0:
        raise QuoteUnavailable(QuoteFailure.INVALID_RESPONSE)
    return value


class KyberSwapQuoteSource:
    """One bounded quote read per ladder step. Never builds a transaction."""

    provider = "kyberswap"

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._clock = clock if clock is not None else SystemClock()
        self._client = httpx.AsyncClient(
            base_url=settings.kyberswap_base_url,
            headers={"Accept": "application/json", "User-Agent": "rh-agents/0.1.0"},
            timeout=httpx.Timeout(
                settings.kyberswap_read_timeout_seconds,
                connect=settings.kyberswap_connect_timeout_seconds,
            ),
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    async def __aenter__(self) -> "KyberSwapQuoteSource":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._client.aclose()

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
        slug = CHAIN_SLUGS.get(chain)
        if slug is None or network != "mainnet":
            raise QuoteUnavailable(QuoteFailure.UNSUPPORTED_CHAIN)
        payload = await self._get(
            f"/{slug}/api/v1/routes",
            {"tokenIn": token_in, "tokenOut": token_out, "amountIn": str(amount_in)},
        )
        # Read after the answer lands, so the name is true. Freshness takes the
        # earlier of this and the provider's own timestamp.
        received_at = self._clock.now()
        try:
            response = RouteResponse.model_validate(payload)
        except ValidationError:
            raise QuoteUnavailable(QuoteFailure.INVALID_RESPONSE) from None
        if response.code in NO_ROUTE_CODES:
            # The provider answered and the answer is about the market.
            raise QuoteUnavailable(QuoteFailure.NO_ROUTE)
        if response.code != 0 or response.data is None:
            raise QuoteUnavailable(QuoteFailure.INVALID_RESPONSE)

        summary = response.data.routeSummary
        if summary.tokenIn.lower() != token_in.lower() or (
            summary.tokenOut.lower() != token_out.lower()
        ):
            raise QuoteUnavailable(QuoteFailure.IDENTITY_MISMATCH)
        if _amount(summary.amountIn) != amount_in:
            raise QuoteUnavailable(QuoteFailure.IDENTITY_MISMATCH)

        hops = [hop for leg in summary.route for hop in leg]
        if not hops:
            raise QuoteUnavailable(QuoteFailure.NO_ROUTE)
        try:
            route = ExecutionRoute(
                # Names the router, never addresses it.
                router=self.provider,
                hops=tuple(
                    RouteHop(
                        venue=hop.exchange,
                        pool=hop.pool.lower(),
                        token_in=hop.tokenIn.lower(),
                        token_out=hop.tokenOut.lower(),
                        amount_in=_amount(hop.swapAmount),
                        amount_out=_amount(hop.amountOut),
                    )
                    for hop in hops
                ),
            )
            return ExecutionQuote(
                chain=chain,
                network=network,
                provider=self.provider,
                token_in=token_in.lower(),
                token_out=token_out.lower(),
                token_in_decimals=token_in_decimals,
                token_out_decimals=token_out_decimals,
                amount_in=amount_in,
                amount_out=_amount(summary.amountOut),
                route=route,
                # The provider's own account of when it priced this, kept
                # beside the moment the answer arrived rather than instead of it.
                quoted_at=datetime.fromtimestamp(summary.timestamp, UTC),
                received_at=received_at,
                provider_amount_in_usd=_usd(summary.amountInUsd),
                # No coherent source block exists to record. Block numbers appear
                # only inside the `poolExtra` and `extra` blobs this adapter
                # never reads, they disagree with each other within a single hop,
                # and the same blobs carry router and permit addresses. Pinning a
                # ladder to a block we cannot coherently read would be a stronger
                # claim than the data supports, so the ladder is time-bounded.
                source_block_number=None,
                # This provider publishes no price-impact figure on either
                # supported chain, verified live. None means absent, and it is
                # never replaced by a number computed elsewhere.
                provider_price_impact_bps=None,
            )
        except ValidationError:
            raise QuoteUnavailable(QuoteFailure.INVALID_RESPONSE) from None

    async def _get(self, path: str, params: dict[str, str]) -> object:
        try:
            async with self._client.stream("GET", path, params=params) as response:
                status = response.status_code
                if status == 429:
                    # Weather, never an empty market. Phase 2B owns the retry.
                    raise QuoteUnavailable(QuoteFailure.RATE_LIMITED)
                if status in (401, 403):
                    raise QuoteUnavailable(QuoteFailure.PROVIDER_UNAVAILABLE)
                if 500 <= status <= 599:
                    raise QuoteUnavailable(QuoteFailure.PROVIDER_UNAVAILABLE)
                # A 400 is how this provider says "no route", and the reason is
                # in the body rather than the status. Refusing it here on the
                # status alone would turn every market fact into an outage, so
                # the body is read and the caller classifies the typed code.
                if status == 404:
                    # The chain slug is not served. A capability we do not have
                    # (or have lost) is never a statement about the market: an
                    # aggregator dropping a chain would otherwise read as every
                    # asset on it having no liquidity.
                    raise QuoteUnavailable(QuoteFailure.UNSUPPORTED_CHAIN)
                if status not in (200, 400):
                    raise QuoteUnavailable(QuoteFailure.INVALID_RESPONSE)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 2_000_000:
                        raise QuoteUnavailable(QuoteFailure.INVALID_RESPONSE)
        except (TimeoutError, httpx.TimeoutException):
            raise QuoteUnavailable(QuoteFailure.TIMEOUT) from None
        except httpx.HTTPError:
            raise QuoteUnavailable(QuoteFailure.PROVIDER_UNAVAILABLE) from None
        try:
            parsed: object = json.loads(body, parse_float=Decimal)
        except (ValueError, UnicodeError, RecursionError, DecimalException):
            raise QuoteUnavailable(QuoteFailure.INVALID_RESPONSE) from None
        if not isinstance(parsed, dict):
            raise QuoteUnavailable(QuoteFailure.INVALID_RESPONSE)
        return parsed
