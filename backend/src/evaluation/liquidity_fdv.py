"""Offline reading of the liquidity/FDV ratio over evidence already recorded.

What the 10 % floor would have done to real markets can only be answered from
figures that were actually observed together. This reads them, and nothing
else: recorded market observations (pool liquidity, price, provider decimals)
and ATLAS's own contract reads (total supply and contract decimals, at a
block), joined only where both describe the same pair at nearly the same time.

**Nothing is fetched.** No provider, no RPC, no model. The database session is
read-only.

**Nothing is estimated.** A market without ATLAS evidence, or whose nearest
observation is too far from the contract read, is counted under the reason it
is UNKNOWN and never given a ratio. A provider's market cap or FDV is not read
at all.

**One arithmetic.** Each comparable pair is judged by
`src.risk.engine.liquidity_fdv_verdict`, the function SENTINEL itself calls,
after the same BUY rounding `market_view` applies (price up, liquidity down, to
the ledger unit), so this cannot disagree with the gate it describes.

    uv run python -m src.evaluation.liquidity_fdv --database NAME [--port 5433]

The connection goes to localhost by name; no URL or credential is printed.
"""

import argparse
import asyncio
import json
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.core.models import RiskLimits
from src.core.numbers import LEDGER_UNIT, fits_ledger, quantize_down, quantize_up
from src.orchestration.workflow.models import HolderDistributionFacts
from src.risk.engine import liquidity_fdv_verdict, scaled_supply

# How far a market observation may lie from the contract read it is paired
# with. SENTINEL's own snapshot bound; anything wider compares two moments.
DEFAULT_MAX_SKEW = timedelta(seconds=30)
MAX_SKEW_CEILING = timedelta(minutes=10)
PASS = "PASS"


@dataclass(frozen=True)
class MarketReading:
    pair_id: str
    observed_at: datetime
    price_usd: Decimal | None
    liquidity_usd: Decimal | None
    provider_decimals: int | None


@dataclass(frozen=True)
class SupplyReading:
    pair_id: str
    observed_at: datetime
    block: int | None
    supply_raw: str | None
    supply_decimals: int | None


@dataclass(frozen=True)
class RatioReading:
    """One verdict: a gate code, PASS, or the reason it is UNKNOWN."""

    pair_id: str
    verdict: str
    ratio: Decimal | None = None
    skew_seconds: float | None = None


def judge(market: MarketReading, supply: SupplyReading, min_ratio: Decimal) -> RatioReading:
    """The gate's verdict on one comparable pair, exactly as a BUY would get it."""
    skew = abs((market.observed_at - supply.observed_at).total_seconds())
    if market.price_usd is None:
        return RatioReading(market.pair_id, "PRICE_UNKNOWN", skew_seconds=skew)
    recorded = market.price_usd
    if recorded < LEDGER_UNIT or not fits_ledger(recorded):
        return RatioReading(market.pair_id, "PRICE_OUTSIDE_ACCOUNTING_PRECISION", skew_seconds=skew)
    price = quantize_up(recorded)
    if not fits_ledger(price):
        # Rounding up carried into a twenty-first integer digit, as `market_view` refuses.
        return RatioReading(market.pair_id, "PRICE_OUTSIDE_ACCOUNTING_PRECISION", skew_seconds=skew)
    liquidity = None
    liquidity_exact = None
    if market.liquidity_usd is not None:
        if not fits_ledger(market.liquidity_usd):
            return RatioReading(
                market.pair_id, "LIQUIDITY_OUTSIDE_ACCOUNTING_PRECISION", skew_seconds=skew
            )
        liquidity = quantize_down(market.liquidity_usd)
        liquidity_exact = liquidity == market.liquidity_usd
    code, ratio = liquidity_fdv_verdict(
        supply=scaled_supply(supply.supply_raw, supply.supply_decimals),
        supply_decimals=supply.supply_decimals,
        provider_decimals=market.provider_decimals,
        price_usd=price,
        price_exact=price == recorded,
        liquidity=liquidity,
        liquidity_exact=liquidity_exact,
        min_ratio=min_ratio,
    )
    if liquidity is None and code is None:
        code = "LIQUIDITY_UNKNOWN"
    return RatioReading(market.pair_id, code or PASS, ratio, skew)


def pair_and_judge(
    markets: Iterable[MarketReading],
    supplies: Iterable[SupplyReading],
    *,
    max_skew: timedelta = DEFAULT_MAX_SKEW,
    min_ratio: Decimal | None = None,
) -> list[RatioReading]:
    """One reading per contract read, and one per market that never had one.

    Each contract read is paired with the market observation of the same pair
    nearest to it in time; beyond `max_skew` it is not comparable. A market
    observed but never read by ATLAS is one `SUPPLY_EVIDENCE_MISSING`, however
    often it was observed.
    """
    if not timedelta(0) <= max_skew <= MAX_SKEW_CEILING:
        raise ValueError("The pairing window must lie between zero and ten minutes")
    floor = RiskLimits().min_liquidity_fdv_ratio if min_ratio is None else min_ratio
    by_pair: dict[str, list[MarketReading]] = {}
    for market in markets:
        by_pair.setdefault(market.pair_id, []).append(market)
    readings: list[RatioReading] = []
    read: set[str] = set()
    for supply in sorted(supplies, key=lambda item: (item.pair_id, item.observed_at)):
        read.add(supply.pair_id)
        candidates = by_pair.get(supply.pair_id, [])
        if not candidates:
            readings.append(RatioReading(supply.pair_id, "MARKET_OBSERVATION_MISSING"))
            continue
        nearest = min(
            candidates,
            key=lambda item: (abs(item.observed_at - supply.observed_at), item.observed_at),
        )
        skew = abs(nearest.observed_at - supply.observed_at)
        if skew > max_skew:
            readings.append(
                RatioReading(
                    supply.pair_id, "EVIDENCE_NOT_COMPARABLE", skew_seconds=skew.total_seconds()
                )
            )
            continue
        readings.append(judge(nearest, supply, floor))
    for pair_id in sorted(set(by_pair) - read):
        readings.append(RatioReading(pair_id, "SUPPLY_EVIDENCE_MISSING"))
    return readings


def quantile(ordered: Sequence[Decimal], fraction: Decimal) -> Decimal:
    """The nearest-rank quantile of an ascending, non-empty sequence."""
    rank = max(1, int((fraction * len(ordered)).to_integral_value(rounding=ROUND_CEILING)))
    return ordered[min(rank, len(ordered)) - 1]


def summarize(readings: Sequence[RatioReading], *, minimum_sample: int = 30) -> dict[str, Any]:
    """Counts per verdict and, only with enough measured ratios, their spread."""
    verdicts = Counter(item.verdict for item in readings)
    ratios = sorted(item.ratio for item in readings if item.ratio is not None)
    spread: dict[str, str] | str
    if len(ratios) < minimum_sample:
        spread = f"INSUFFICIENT_SAMPLE ({len(ratios)} < {minimum_sample})"
    else:
        spread = {
            name: str(quantile(ratios, Decimal(fraction)))
            for name, fraction in (
                ("min", "0"),
                ("p10", "0.10"),
                ("p25", "0.25"),
                ("median", "0.50"),
                ("p75", "0.75"),
                ("p90", "0.90"),
                ("max", "1"),
            )
        }
    return {
        "readings": len(readings),
        "pairs": len({item.pair_id for item in readings}),
        "verdicts": dict(sorted(verdicts.items())),
        "measured_ratios": len(ratios),
        "ratio_spread": spread,
    }


def token_age_at_first_sight(rows: Iterable[tuple[datetime, datetime | None]]) -> dict[str, Any]:
    """How old pools were when first seen, from the provider's own creation time.

    A watch without a recorded creation time is UNKNOWN, never estimated from
    discovery. Negative ages (a creation time after first sight) are counted
    apart, as inconsistent records.
    """
    known: list[Decimal] = []
    unknown = inconsistent = 0
    for first_seen, created in rows:
        if created is None:
            unknown += 1
        elif created > first_seen:
            inconsistent += 1
        else:
            known.append(Decimal(str((first_seen - created).total_seconds())))
    known.sort()
    return {
        "watches": len(known) + unknown + inconsistent,
        "unknown": unknown,
        "inconsistent": inconsistent,
        "median_seconds": str(quantile(known, Decimal("0.5"))) if known else "UNKNOWN",
    }


# ------------------------------------------------------------ recorded rows


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def market_reading(pair_id: str, observed_at: datetime, payload: dict[str, Any]) -> MarketReading:
    """A recorded observation's figures. Anything unreadable or unavailable is None."""
    price = payload.get("price") or {}
    liquidity = payload.get("liquidity") or {}
    base = (payload.get("pair") or {}).get("base") or {}
    decimals = base.get("decimals")
    return MarketReading(
        pair_id=pair_id,
        observed_at=observed_at,
        price_usd=_decimal(price.get("value_usd")) if price.get("status") == "AVAILABLE" else None,
        liquidity_usd=(
            _decimal(liquidity.get("value_usd")) if liquidity.get("status") == "AVAILABLE" else None
        ),
        provider_decimals=decimals if isinstance(decimals, int) else None,
    )


def supply_reading(
    pair_id: str, observed_at: datetime, payload: dict[str, Any]
) -> SupplyReading | None:
    """ATLAS's contract read from one ONCHAIN envelope, or None when it has none."""
    intelligence = payload.get("intelligence")
    if not isinstance(intelligence, dict) or intelligence.get("holders") is None:
        return None
    try:
        holders = HolderDistributionFacts.model_validate(intelligence["holders"])
    except ValidationError:
        return None
    block = intelligence.get("block_number")
    return SupplyReading(
        pair_id=pair_id,
        observed_at=holders.observed_at,
        block=block if isinstance(block, int) else None,
        supply_raw=holders.total_supply_raw,
        supply_decimals=holders.total_supply_decimals,
    )


async def load(engine: AsyncEngine, *, chain: str) -> dict[str, Any]:
    """Read everything this needs, in one read-only transaction."""
    async with engine.connect() as connection:
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        columns = await connection.run_sync(
            lambda sync: {
                table: {item["name"] for item in inspect(sync).get_columns(table)}
                for table in ("discovery_watches",)
                if inspect(sync).has_table(table)
            }
        )
        markets = [
            market_reading(row.pair_id, row.observed_at, row.payload)
            for row in await connection.execute(
                text(
                    "SELECT pair_id, observed_at, payload FROM market_observations"
                    " WHERE NOT is_fixture AND chain = :chain"
                ),
                {"chain": chain},
            )
        ]
        supplies: list[SupplyReading] = []
        unreadable = 0
        for row in await connection.execute(
            text(
                "SELECT c.market_key, e.observed_at, e.payload FROM trade_case_evidence e"
                " JOIN trade_cases c ON c.id = e.trade_case_id"
                " WHERE e.evidence_type = 'ONCHAIN' AND c.chain = :chain"
            ),
            {"chain": chain},
        ):
            reading = supply_reading(row.market_key, row.observed_at, row.payload)
            if reading is None:
                unreadable += 1
            else:
                supplies.append(reading)
        ages: list[tuple[datetime, datetime | None]] | None = None
        if "pool_created_at" in columns.get("discovery_watches", set()):
            ages = [
                (row.first_seen_at, row.pool_created_at)
                for row in await connection.execute(
                    text(
                        "SELECT first_seen_at, pool_created_at FROM discovery_watches"
                        " WHERE NOT is_fixture AND chain = :chain"
                    ),
                    {"chain": chain},
                )
            ]
        await connection.rollback()
    return {
        "markets": markets,
        "supplies": supplies,
        "onchain_without_supply": unreadable,
        "ages": ages,
    }


async def report(engine: AsyncEngine, *, chain: str, max_skew: timedelta) -> dict[str, Any]:
    loaded = await load(engine, chain=chain)
    readings = pair_and_judge(loaded["markets"], loaded["supplies"], max_skew=max_skew)
    markets: list[MarketReading] = loaded["markets"]
    return {
        "chain": chain,
        "max_skew_seconds": max_skew.total_seconds(),
        "market_observations": len(markets),
        "with_liquidity": sum(1 for item in markets if item.liquidity_usd is not None),
        "with_price": sum(1 for item in markets if item.price_usd is not None),
        "with_provider_decimals": sum(1 for item in markets if item.provider_decimals is not None),
        "onchain_evidence_with_supply": len(loaded["supplies"]),
        "onchain_evidence_without_supply": loaded["onchain_without_supply"],
        "ratio": summarize(readings),
        "token_age_at_first_sight": (
            "UNKNOWN (no pool_created_at column at this revision)"
            if loaded["ages"] is None
            else token_age_at_first_sight(loaded["ages"])
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", required=True, help="a local database name")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--chain", default="robinhood")
    parser.add_argument("--max-skew-seconds", type=int, default=30)
    arguments = parser.parse_args(argv)
    if not arguments.database.replace("_", "").isalnum() or not 0 < arguments.port < 65536:
        parser.error("database must be a plain name and port a TCP port")
    engine = create_async_engine(
        f"postgresql+asyncpg://localhost:{arguments.port}/{arguments.database}",
        connect_args={"server_settings": {"default_transaction_read_only": "on"}},
    )

    async def run() -> dict[str, Any]:
        try:
            return await report(
                engine,
                chain=arguments.chain,
                max_skew=timedelta(seconds=arguments.max_skew_seconds),
            )
        finally:
            await engine.dispose()

    json.dump(asyncio.run(run()), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
