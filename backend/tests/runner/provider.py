"""The market provider as HTTP responses, and nothing else substituted.

One external boundary: the bytes an HTTPS response would carry. Above it the
transport, the network directory, `GeckoTerminalAdapter`, `normalize`, the
`MarketRecorder`, `MarketReader`, intake, the specialists, SENTINEL and the
executor are all production objects.

The identities here are deliberately the ones the ATLAS, risk-data and ANCHOR
suites already use — chain `robinhood`, token `0xa1…`, payment asset `0xb2…`,
pool `0xe5…` — so a market that arrives through this provider is the same market
those fixtures describe, rather than a second notion of which token this is.

**A fixture is never evidence that the real provider works.** It proves what this
system does with an answer of that shape, which is a different and much smaller
claim.
"""

import json
from decimal import Decimal
from typing import Any

import httpx

from tests.atlas.conftest import QUOTE, TOKEN

CHAIN = "robinhood"
NETWORK_ID = "robinhood"
VENUE = "uniswap-v3"

# The traded market, and the market that prices the payment asset in dollars.
# ANCHOR needs the second: it sizes a ladder in the token that would actually be
# spent, so it needs that token's own USD reading and refuses to infer one.
POOL = "0x" + "e5" * 20
PAYMENT_POOL = "0x" + "ff" * 20
PAYMENT_QUOTE = "0x" + "dd" * 20

NETWORKS = {
    "data": [
        {
            "id": "bsc",
            "type": "network",
            "attributes": {
                "name": "BNB Chain",
                "coingecko_asset_platform_id": "binance-smart-chain",
            },
        },
        {
            "id": NETWORK_ID,
            "type": "network",
            "attributes": {"name": "Robinhood", "coingecko_asset_platform_id": "robinhood"},
        },
    ],
    "links": {"next": None},
}


def token(address: str, symbol: str, decimals: int = 18) -> dict[str, Any]:
    return {
        "id": f"{NETWORK_ID}_{address}",
        "type": "token",
        "attributes": {"address": address, "symbol": symbol, "decimals": decimals},
    }


def pool(
    address: str,
    *,
    base: str,
    quote: str,
    price: Decimal | str | None,
    liquidity: str = "750000",
    volume: str = "125000",
    venue: str = VENUE,
) -> dict[str, Any]:
    """One pool resource, as the documented schema carries it.

    `price=None` is a real answer rather than an omission: a provider that
    cannot price a pool says so, and the observation that is recorded is then
    unavailable — which is exactly the contract that stops an unusable new
    reading from falling back to a usable old one.
    """
    return {
        "id": f"{NETWORK_ID}_{address}",
        "type": "pool",
        "attributes": {
            "address": address,
            "name": "PAIR",
            "base_token_price_usd": None if price is None else str(price),
            "reserve_in_usd": liquidity,
            "volume_usd": {"h24": volume},
        },
        "relationships": {
            "base_token": {"data": {"type": "token", "id": f"{NETWORK_ID}_{base}"}},
            "quote_token": {"data": {"type": "token", "id": f"{NETWORK_ID}_{quote}"}},
            "dex": {"data": {"id": venue, "type": "dex"}},
        },
    }


def traded(price: Decimal | str) -> dict[str, Any]:
    return pool(POOL, base=TOKEN, quote=QUOTE, price=price)


def payment(price: Decimal | str = "1") -> dict[str, Any]:
    """A pool in which the payment asset is the thing being priced."""
    return pool(PAYMENT_POOL, base=QUOTE, quote=PAYMENT_QUOTE, price=price, liquidity="900000")


def document(pools: list[dict[str, Any]]) -> dict[str, Any]:
    """A pools document with exactly the resources its pools refer to."""
    symbols = {TOKEN: "TEST", QUOTE: "PAY", PAYMENT_QUOTE: "OTHER"}
    included: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in pools:
        for relation in ("base_token", "quote_token"):
            resource = item["relationships"][relation]["data"]["id"]
            address = resource.removeprefix(f"{NETWORK_ID}_")
            if resource in seen:
                continue
            seen.add(resource)
            included.append(token(address, symbols.get(address, "SYM")))
    return {"data": pools, "included": included}


class MarketProvider:
    """A scripted provider, answering the two reads this system performs.

    Records every request it is given, so a test can assert what was *actually*
    asked for rather than inferring it from what was stored — the difference
    between proving a budget bounded the spend and proving it bounded the
    result.
    """

    def __init__(
        self,
        *,
        discovery: list[dict[str, Any]] | None = None,
        targeted: list[dict[str, Any]] | None = None,
        status: int | None = None,
        discovery_status: int | None = None,
        networks: dict[str, Any] | None = None,
    ) -> None:
        self.discovery = discovery if discovery is not None else []
        # What `pools/multi` knows about, by address. Anything not here is a
        # market the provider does not return, which is a real answer.
        self.targeted = {
            item["attributes"]["address"]: item for item in (targeted if targeted else [])
        }
        self.status = status
        # A failure on the discovery read alone, so a pass can confirm some
        # recordings and then meet an error — which is a different situation
        # from one where nothing was written at all.
        self.discovery_status = discovery_status
        self.networks = networks if networks is not None else NETWORKS
        self.requests: list[httpx.Request] = []
        self.paths: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept"] == "application/json;version=20230203"
        assert request.headers["User-Agent"] == "rh-agents/0.1.0"
        assert "authorization" not in request.headers
        self.requests.append(request)
        path = request.url.path
        self.paths.append(path)
        if path.endswith("/networks"):
            return self._json(self.networks)
        if self.status is not None:
            return httpx.Response(self.status, text=json.dumps({"errors": [{"status": "boom"}]}))
        if path.endswith("new_pools"):
            if self.discovery_status is not None:
                return httpx.Response(
                    self.discovery_status, text=json.dumps({"errors": [{"status": "boom"}]})
                )
            return self._json(document(self.discovery))
        if "/pools/multi/" in path:
            wanted = path.rsplit("/", 1)[-1].split(",")
            self.asked = wanted
            found = [self.targeted[item] for item in wanted if item in self.targeted]
            return self._json(document(found))
        raise AssertionError(f"unexpected provider path {path}")

    @property
    def multi_requests(self) -> list[str]:
        return [item for item in self.paths if "/pools/multi/" in item]

    @property
    def discovery_requests(self) -> list[str]:
        return [item for item in self.paths if item.endswith("new_pools")]

    @staticmethod
    def _json(payload: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, text=json.dumps(payload, default=str))
