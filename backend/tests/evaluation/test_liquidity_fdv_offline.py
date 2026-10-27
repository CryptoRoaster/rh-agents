"""The offline ratio reading uses only figures observed together, and SENTINEL's arithmetic."""

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.evaluation.liquidity_fdv import (
    MarketReading,
    SupplyReading,
    judge,
    market_reading,
    pair_and_judge,
    quantile,
    summarize,
    supply_reading,
    token_age_at_first_sight,
)
from tests.riskdata.conftest import holder_block

AT = datetime(2026, 10, 27, 12, tzinfo=UTC)
FLOOR = Decimal("0.10")
# One million whole tokens at 18 decimals.
SUPPLY = "1000000000000000000000000"


def market(liquidity="100000", price="1", *, pair="p", at=AT, decimals=18):
    return MarketReading(
        pair_id=pair,
        observed_at=at,
        price_usd=None if price is None else Decimal(price),
        liquidity_usd=None if liquidity is None else Decimal(liquidity),
        provider_decimals=decimals,
    )


def supply(raw=SUPPLY, decimals=18, *, pair="p", at=AT):
    return SupplyReading(
        pair_id=pair, observed_at=at, block=1, supply_raw=raw, supply_decimals=decimals
    )


@pytest.mark.parametrize(
    ("liquidity", "price", "verdict"),
    [
        # FDV 1 000 000 USD throughout.
        ("100000", "1", "PASS"),  # exactly 10 %
        ("99999.999999999999999999", "1", "LIQUIDITY_FDV_RATIO_LIMIT"),
        ("1000000", "1", "PASS"),  # exactly 100 %, exact figures
        ("1000000.000000000000000001", "1", "LIQUIDITY_FDV_RATIO_UNVERIFIED"),
        # A pool floored by the BUY rounding leaves exactly 100 % undecidable.
        ("1000000.0000000000000000001", "1", "LIQUIDITY_FDV_RATIO_UNDECIDABLE"),
        # A price rounded up still bounds the true ratio below 100 %.
        ("1000000", "1.0000000000000000001", "PASS"),
    ],
)
def test_the_bounds_are_the_gates_own(liquidity, price, verdict):
    reading = judge(market(liquidity, price), supply(), FLOOR)
    assert reading.verdict == verdict


def test_missing_figures_stay_unknown_and_get_no_ratio():
    assert judge(market(price=None), supply(), FLOOR).verdict == "PRICE_UNKNOWN"
    assert judge(market(liquidity=None), supply(), FLOOR).verdict == "LIQUIDITY_UNKNOWN"
    assert judge(market(), supply(raw=None), FLOOR).verdict == "FDV_UNKNOWN"
    assert judge(market(), supply(decimals=None), FLOOR).verdict == "FDV_UNKNOWN"
    assert judge(market(), supply(raw="0"), FLOOR).verdict == "FDV_UNKNOWN"
    assert judge(market(decimals=6), supply(), FLOOR).verdict == "FDV_DECIMALS_MISMATCH"
    assert judge(market(price="0.0000000000000000001"), supply(), FLOOR).verdict == (
        "PRICE_OUTSIDE_ACCOUNTING_PRECISION"
    )
    for reading in (
        judge(market(price=None), supply(), FLOOR),
        judge(market(), supply(raw=None), FLOOR),
    ):
        assert reading.ratio is None


def test_only_close_observations_are_compared():
    readings = pair_and_judge(
        [market(at=AT - timedelta(seconds=31)), market(pair="q"), market(pair="r", at=AT)],
        [supply(), supply(pair="r", at=AT + timedelta(seconds=30))],
    )
    by_pair = {item.pair_id: item for item in readings}
    assert by_pair["p"].verdict == "EVIDENCE_NOT_COMPARABLE"
    assert by_pair["p"].ratio is None
    assert by_pair["q"].verdict == "SUPPLY_EVIDENCE_MISSING"
    assert by_pair["r"].verdict == "PASS"
    assert by_pair["r"].ratio == Decimal("0.1")


def test_the_nearest_observation_is_the_one_compared():
    readings = pair_and_judge(
        [
            market("50000", at=AT - timedelta(seconds=20)),
            market("200000", at=AT + timedelta(seconds=2)),
        ],
        [supply()],
    )
    assert [(item.verdict, item.ratio) for item in readings] == [("PASS", Decimal("0.2"))]


def test_a_contract_read_without_any_observation_is_its_own_unknown():
    readings = pair_and_judge([], [supply(pair="lonely")])
    assert [item.verdict for item in readings] == ["MARKET_OBSERVATION_MISSING"]


def test_the_pairing_window_is_bounded():
    with pytest.raises(ValueError):
        pair_and_judge([], [], max_skew=timedelta(minutes=11))
    with pytest.raises(ValueError):
        pair_and_judge([], [], max_skew=timedelta(seconds=-1))


def test_a_spread_needs_a_sample():
    few = pair_and_judge([market(pair=f"p{i}") for i in range(3)], [])
    assert summarize(few)["ratio_spread"] == "INSUFFICIENT_SAMPLE (0 < 30)"
    assert summarize(few)["verdicts"] == {"SUPPLY_EVIDENCE_MISSING": 3}

    many = pair_and_judge(
        [market(str(10000 * (i + 10)), pair=f"p{i:02}") for i in range(40)],
        [supply(pair=f"p{i:02}") for i in range(40)],
    )
    spread = summarize(many)["ratio_spread"]
    assert Decimal(spread["min"]) == Decimal("0.1")
    assert Decimal(spread["max"]) == Decimal("0.49")


def test_quantiles_are_nearest_rank():
    ordered = [Decimal(item) for item in range(1, 11)]
    assert quantile(ordered, Decimal("0")) == 1
    assert quantile(ordered, Decimal("0.5")) == 5
    assert quantile(ordered, Decimal("0.9")) == 9
    assert quantile(ordered, Decimal("1")) == 10


def test_token_age_is_never_estimated():
    ages = token_age_at_first_sight(
        [
            (AT, AT - timedelta(minutes=4)),
            (AT, None),
            (AT, AT + timedelta(seconds=1)),
        ]
    )
    assert ages == {"watches": 3, "unknown": 1, "inconsistent": 1, "median_seconds": "240.0"}
    assert token_age_at_first_sight([(AT, None)])["median_seconds"] == "UNKNOWN"


def test_recorded_rows_are_read_strictly():
    payload = {
        "price": {"status": "AVAILABLE", "value_usd": "1.25"},
        "liquidity": {"status": "UNKNOWN", "value_usd": "750000"},
        "pair": {"base": {"decimals": 18}},
    }
    reading = market_reading("p", AT, payload)
    assert reading.price_usd == Decimal("1.25")
    # An unavailable figure is not read, whatever value sits beside it.
    assert reading.liquidity_usd is None
    assert (
        market_reading("p", AT, {"price": {"status": "AVAILABLE", "value_usd": "NaN"}}).price_usd
        is None
    )

    holders = holder_block(AT).model_dump(mode="json")
    found = supply_reading("p", AT, {"intelligence": {"holders": holders, "block_number": 7}})
    assert (found.supply_raw, found.supply_decimals, found.block) == (
        holders["total_supply_raw"],
        18,
        7,
    )
    assert supply_reading("p", AT, {"intelligence": None}) is None
    assert supply_reading("p", AT, {"intelligence": {"holders": {"bad": 1}}}) is None


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="reads PostgreSQL")
async def test_the_report_reads_a_database_without_writing(risk_db):  # noqa: F811
    from src.evaluation.liquidity_fdv import report

    engine, _ = risk_db
    result = await report(engine, chain="robinhood", max_skew=timedelta(seconds=30))
    assert result["market_observations"] == 0
    assert result["ratio"]["measured_ratios"] == 0


from tests.riskrequest.conftest import risk_db  # noqa: E402,F401
