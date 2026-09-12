"""GeckoTerminal pool OHLCV, normalized into closed typed bars.

Verified against the live V2 contract (`version=20230203`) while writing this
module rather than assumed from memory. What was confirmed, and what each finding
forced:

**Orientation is a request parameter, and getting it wrong is not obvious.** On
the Robinhood NVDA/USDG pool, one interval returned three different closes:
219.483735394483 for `currency=usd&token=base`, 1.00017330493874 for
`token=quote` (the response swaps which token it calls base), and
219.151021699933 for `currency=token` — NVDA priced in USDG rather than dollars.
The third is within 0.2% of the correct value because the quote asset happens to
be a dollar stablecoin, so a magnitude check would pass it. Both parameters are
therefore sent explicitly even though both are the documented defaults, and the
response's own `meta.base.address` is compared against the market's base token.
A vendor changing a default cannot silently invert a series.

**The newest bar is still forming, and the provider does not say so.** At
07:35Z the newest hourly bar opened at 07:00Z, and its close was byte-identical
to the pool's live `base_token_price_usd` on both Robinhood and BSC. It is the
current price wearing a candle's shape. It is dropped: a forming interval
presented as settled structure would understate the range and invent a high and
a low that the interval has not finished making.

**Empty intervals are omitted, not flattened.** `include_empty_intervals`
defaults to false and is sent explicitly as false. An interval in which nobody
traded is a fact; a synthesized flat bar carrying the previous close would be
fabricated structure, which is the precise thing this phase exists to prevent.
The gaps are recovered from the timestamps and reported.

**Ordering is newest-first.** Confirmed on both chains. Normalization sorts
ascending rather than trusting the order, so a provider change reorders nothing
downstream.
"""

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, ValidationError, field_validator

from src.core.clock import Clock, SystemClock
from src.core.config import Settings
from src.markets.geckoterminal.dto import DTO, Text
from src.markets.geckoterminal.errors import (
    AuthenticationError,
    BudgetError,
    ClientError,
    ContractError,
    IdentityError,
    ProviderError,
    RateLimitError,
    UnavailableError,
    UnsupportedNetworkError,
)
from src.markets.geckoterminal.networks import Chain, NetworkDirectory, validate
from src.markets.geckoterminal.transport import GeckoTerminalTransport
from src.markets.history import (
    USD_PER_BASE_UNIT,
    MarketBar,
    MarketHistory,
    MarketHistoryUnavailable,
    bar_from_row,
    coverage_for,
    empty_history,
    interval_seconds,
)
from src.markets.models import MarketIdentity

# Sent explicitly although both are the documented defaults. A default belongs to
# the vendor and can change; a parameter belongs to the caller. `usd` fixes the
# numerator and `base` fixes the denominator, which together are the only reason
# the series can be called USD per base unit.
CURRENCY = "usd"
TOKEN_SIDE = "base"

# How each provider failure reaches a consumer of the port. The port's contract
# is that it raises MarketHistoryUnavailable with a safe code, so provider
# exception types stop here: a caller that had to catch GeckoTerminal's classes
# would be coupled to this adapter, and one that caught nothing would turn a rate
# limit into an unexplained internal error.
#
# Emphatically none of these becomes an empty series. A rate limit is not an
# untraded market, an unsupported network is not a pool without bars, and a
# transport failure is not insufficient history. Each of those confusions would
# be read downstream as a fact about the market.
HISTORY_FAILURES: tuple[tuple[type[ProviderError], str], ...] = (
    (RateLimitError, "MARKET_HISTORY_PROVIDER_RATE_LIMITED"),
    (AuthenticationError, "MARKET_HISTORY_PROVIDER_NOT_AUTHORIZED"),
    (BudgetError, "MARKET_HISTORY_REQUEST_BUDGET_EXHAUSTED"),
    (UnsupportedNetworkError, "MARKET_HISTORY_NETWORK_UNSUPPORTED"),
    # IdentityError subclasses ContractError, so it is listed first.
    (IdentityError, "MARKET_HISTORY_PROVIDER_IDENTITY"),
    (ContractError, "MARKET_HISTORY_PROVIDER_CONTRACT"),
    # A 4xx the provider chose to return: it rejected this request and will
    # reject the identical one again, so it is not weather to be waited out.
    (ClientError, "MARKET_HISTORY_PROVIDER_REJECTED"),
    (UnavailableError, "MARKET_HISTORY_PROVIDER_UNAVAILABLE"),
)


def history_failure(error: ProviderError) -> MarketHistoryUnavailable:
    """Translate a provider failure into the port's own safe vocabulary."""
    for provider_error, reason_code in HISTORY_FAILURES:
        if isinstance(error, provider_error):
            return MarketHistoryUnavailable(reason_code)
    return MarketHistoryUnavailable("MARKET_HISTORY_PROVIDER_UNAVAILABLE")


class OhlcvAttributes(DTO):
    # Rows stay untyped here and are normalized one at a time, so one malformed
    # row produces a precise refusal rather than an opaque schema error.
    ohlcv_list: list[object] = Field(max_length=1000)


class OhlcvData(DTO):
    id: Text
    type: Literal["ohlcv_request_response"]
    attributes: OhlcvAttributes


class OhlcvToken(DTO):
    address: Annotated[str, Field(strict=True, pattern=r"^0x[0-9a-fA-F]{40}$")]
    symbol: Text

    @field_validator("address")
    @classmethod
    def lowercase(cls, value: str) -> str:
        return value.lower()


class OhlcvMeta(DTO):
    base: OhlcvToken
    quote: OhlcvToken


class OhlcvResponse(DTO):
    data: OhlcvData
    # The provider states which token it priced. That statement is what makes the
    # orientation checkable rather than merely requested.
    meta: OhlcvMeta


def token_address(asset_id: str) -> str:
    """The bare address out of a chain-scoped asset id, lowercased."""
    return asset_id.rsplit(":", 1)[-1].lower()


def pool_address(pair_id: str) -> str:
    return pair_id.rsplit(":", 1)[-1].lower()


class GeckoTerminalOhlcvSource:
    """One bounded history read per context acquisition.

    The transport carries its own request budget, so this source cannot become a
    retry amplifier: it issues exactly one logical request and returns, and the
    Phase 2B runtime remains the only owner of retries.
    """

    provider = "geckoterminal"
    is_fixture = False

    def __init__(
        self,
        transport: GeckoTerminalTransport,
        directory: NetworkDirectory,
        chain: Chain,
        settings: Settings,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._transport = transport
        self._directory = directory
        self._chain = chain
        self._clock = clock if clock is not None else SystemClock()
        self._settings = settings

    async def history(
        self, identity: object, *, timeframe: str, aggregate: int, bars: int
    ) -> MarketHistory:
        """One bounded read, or a typed refusal in the port's own vocabulary."""
        try:
            return await self._history(identity, timeframe, aggregate, bars)
        except ProviderError as error:
            raise history_failure(error) from None

    async def _history(
        self, identity: object, timeframe: str, aggregate: int, bars: int
    ) -> MarketHistory:
        if not isinstance(identity, MarketIdentity):
            raise IdentityError()
        if identity.chain != self._chain.name:
            raise IdentityError()
        step = interval_seconds(timeframe, aggregate)
        network_id = await self._directory.resolve(self._chain)
        # One extra bar is requested because the newest one is discarded unread.
        payload = await self._transport.get(
            f"networks/{network_id}/pools/{pool_address(identity.pair_id)}/ohlcv/{timeframe}",
            {
                "aggregate": aggregate,
                "limit": min(bars + 1, 1000),
                "currency": CURRENCY,
                "token": TOKEN_SIDE,
                "include_empty_intervals": "false",
            },
        )
        fetched_at = self._clock.now()
        response = validate(OhlcvResponse, payload)
        self._verify_orientation(response.meta, identity)
        rows = response.data.attributes.ohlcv_list
        return self._normalize(rows, identity, timeframe, aggregate, step, bars, fetched_at)

    def _verify_orientation(self, meta: OhlcvMeta, identity: MarketIdentity) -> None:
        """The series must price the market's base asset, in dollars.

        `token=quote` returns a response whose own `meta.base` is the quote
        asset, so this comparison catches an inverted series structurally rather
        than by inspecting whether the numbers look plausible — which, against a
        dollar-stablecoin quote, they would.
        """
        if meta.base.address != token_address(identity.base_asset_id):
            raise IdentityError()
        if meta.quote.address != token_address(identity.quote_asset_id):
            raise IdentityError()
        if meta.base.address == meta.quote.address:
            raise IdentityError()

    def _normalize(
        self,
        rows: list[object],
        identity: MarketIdentity,
        timeframe: str,
        aggregate: int,
        step: int,
        requested: int,
        fetched_at: datetime,
    ) -> MarketHistory:
        try:
            parsed = [bar_from_row(row, step=step) for row in rows]
        except ValueError:
            # A malformed bar is a contract violation, not a thin market.
            raise ContractError() from None
        ordered = sorted(parsed, key=lambda bar: bar.opened_at)
        openings = {bar.opened_at for bar in ordered}
        if len(openings) != len(ordered):
            # The same interval twice is incoherent, and choosing between the two
            # copies would be inventing which one the market actually did.
            raise ContractError()
        # An interval that has not begun is not an interval still being written,
        # and dropping it silently alongside the forming bar would let a provider
        # clock fault look like an ordinary short window. The tolerance is one
        # interval, because at an exact boundary the provider's clock and ours
        # can legitimately disagree by milliseconds — a bar opening a whole
        # interval ahead cannot be explained that way.
        if any(bar.opened_at >= fetched_at + timedelta(seconds=step) for bar in ordered):
            raise ContractError()
        closed = self._closed_only(ordered, fetched_at)[-requested:]
        if not closed:
            return empty_history(
                pair_id=identity.pair_id,
                provider=self.provider,
                chain=identity.chain,
                network=identity.network,
                venue=identity.venue,
                base_asset_id=identity.base_asset_id,
                quote_asset_id=identity.quote_asset_id,
                timeframe=timeframe,
                aggregate=aggregate,
                requested_bars=requested,
                fetched_at=fetched_at,
                is_fixture=self.is_fixture,
            )
        try:
            return MarketHistory(
                pair_id=identity.pair_id,
                provider=self.provider,
                chain=identity.chain,
                network=identity.network,
                venue=identity.venue,
                base_asset_id=identity.base_asset_id,
                quote_asset_id=identity.quote_asset_id,
                price_basis=USD_PER_BASE_UNIT,
                timeframe=timeframe,  # type: ignore[arg-type]
                aggregate=aggregate,
                bars=tuple(closed),
                requested_bars=requested,
                # Derived from the bars, never declared by the caller.
                coverage=coverage_for(closed, requested, step),
                observed_at=closed[-1].closed_at,
                fetched_at=fetched_at,
                is_fixture=self.is_fixture,
            )
        except ValidationError:
            # Series-level violations — an opening off the interval grid, a bar
            # carrying the wrong interval — must leave as a safe provider code
            # like every other contract failure. A raw validation error here
            # would be upstream detail escaping the boundary.
            raise ContractError() from None

    @staticmethod
    def _closed_only(ordered: list[MarketBar], fetched_at: datetime) -> list[MarketBar]:
        """Drop any interval that had not ended when the data was retrieved.

        The provider marks nothing, so the interval's own arithmetic decides: a
        bar whose end has not yet passed is still being written, whatever its
        close currently says.
        """
        return [bar for bar in ordered if bar.closed_at <= fetched_at]
