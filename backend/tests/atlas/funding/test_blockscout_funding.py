"""BlockscoutFundingSource against the documented v2 address-transactions contract.

``GET /{chain_id}/api/v2/addresses/{hash}/transactions?filter=from``: newest
first, 50 per page, keyset cursors in ``next_page_params``. No test performs
I/O; payloads follow the documented schema.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from src.agents.atlas.funding.models import FundingCoverage
from src.agents.atlas.models import AtlasSourceFailure
from src.agents.atlas.sources.blockscout_funding import (
    MAX_FUNDING_PAGES,
    BlockscoutFundingConfig,
    BlockscoutFundingSource,
)
from src.markets.models import Availability
from tests.atlas.fake_http import RecordingRoutes, json_response

CREATOR = "0x" + "e7" * 20
CREATED = 1_000
SNAPSHOT = 5_000
# Synthetic block times: one block per minute, so block arithmetic is time arithmetic.
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def at(block: int) -> datetime:
    return EPOCH + timedelta(minutes=block)


def stamp(block: int | None) -> str | None:
    return None if block is None else at(block).isoformat().replace("+00:00", "Z")


CONFIG = BlockscoutFundingConfig(
    base_url="https://api.blockscout.test", chain_id=4663, api_key="proapi_testkey", max_pages=3
)


def item(
    index: int,
    block: int | None,
    *,
    to: str | None = "default",
    value: str = "1000000000000000",
    status: str | None = "ok",
    sender: str = CREATOR,
    position: int = 0,
    timestamp: object = "auto",
):
    return {
        "timestamp": stamp(block) if timestamp == "auto" else timestamp,
        "hash": "0x" + format(index, "064x"),
        "block_number": block,
        "from": {"hash": sender},
        "to": None
        if to is None
        else {"hash": "0x" + format(index, "040x") if to == "default" else to},
        "value": value,
        "status": status,
        "position": position,
        "result": "success",
    }


def page(items, next_params=None):
    return {"items": items, "next_page_params": next_params}


def cursor(block: int, index: int = 0):
    return {"block_number": block, "index": index, "items_count": 50}


def source(
    pages: list[object], status: int = 200
) -> tuple[BlockscoutFundingSource, RecordingRoutes]:
    answers = iter(pages)

    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(next(answers), status)

    routes = RecordingRoutes({f"/addresses/{CREATOR}/transactions": handler})
    return (
        BlockscoutFundingSource(
            config=CONFIG, chain="robinhood", transport_factory=routes.transport_factory()
        ),
        routes,
    )


async def read(
    src: BlockscoutFundingSource, chain: str = "robinhood", history_until: datetime | None = None
):
    return await src.funding_transactions(
        chain, CREATOR, CREATED, SNAPSHOT, history_until=history_until
    )


# ---------------------------------------------------------------- contract


async def test_the_documented_request_is_made_with_header_auth() -> None:
    src, routes = source([page([item(1, 4_000)])])
    result = await read(src)
    assert result.status is Availability.AVAILABLE
    (request,) = routes.requests
    assert request.url.path == f"/4663/api/v2/addresses/{CREATOR}/transactions"
    assert request.url.params["filter"] == "from"
    assert request.headers["Authorization"] == "Bearer proapi_testkey"
    assert "apikey" not in str(request.url) and "proapi_testkey" not in str(request.url)


async def test_a_list_that_ends_is_complete() -> None:
    src, _ = source([page([item(2, 4_500), item(1, 1_200)])])
    result = await read(src)
    assert result.coverage is FundingCoverage.COMPLETE
    assert [tx.block_number for tx in result.transactions] == [4_500, 1_200]
    assert result.requests_made == 1


async def test_reaching_a_transaction_older_than_creation_is_complete() -> None:
    src, routes = source(
        [
            page([item(3, 4_900), item(2, 3_000)], cursor(3_000)),
            page([item(1, 2_000), item(0, 900)], cursor(900)),  # 900 < creation
        ]
    )
    result = await read(src)
    assert result.coverage is FundingCoverage.COMPLETE
    assert [tx.block_number for tx in result.transactions] == [4_900, 3_000, 2_000]
    assert len(routes.requests) == 2
    assert routes.requests[1].url.params["block_number"] == "3000"
    assert routes.requests[1].url.params["filter"] == "from"


async def test_a_page_budget_that_runs_out_is_a_lower_bound() -> None:
    src, _ = source(
        [
            page([item(5, 4_900)], cursor(4_900)),
            page([item(4, 4_800)], cursor(4_800)),
            page([item(3, 4_700)], cursor(4_700)),
        ]
    )
    result = await read(src)
    assert result.status is Availability.AVAILABLE
    assert result.coverage is FundingCoverage.LOWER_BOUND
    assert len(result.transactions) == 3
    assert result.requests_made == 3


async def test_transactions_after_the_snapshot_are_not_returned() -> None:
    src, _ = source([page([item(2, SNAPSHOT + 10), item(1, 4_000)])])
    result = await read(src)
    assert [tx.block_number for tx in result.transactions] == [4_000]


async def test_pending_transactions_are_skipped() -> None:
    src, _ = source([page([item(2, None), item(1, 4_000)])])
    result = await read(src)
    assert [tx.block_number for tx in result.transactions] == [4_000]


async def test_failed_creation_and_zero_rows_are_normalized_not_dropped() -> None:
    src, _ = source(
        [page([item(3, 4_000, status="error"), item(2, 3_900, to=None), item(1, 3_800, value="0")])]
    )
    result = await read(src)
    by_block = {tx.block_number: tx for tx in result.transactions}
    assert by_block[4_000].succeeded is False
    assert by_block[3_900].recipient is None
    assert by_block[3_800].native_value_raw == 0


# ----------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    "bad",
    [
        {"from": {"hash": "0x" + "12" * 20}},  # not the asked-for sender
        {"from": {"hash": "not-an-address"}},
        {"hash": "0x1234"},
        {"block_number": "-1"},
        {"block_number": 1.5},
        {"value": "1e18"},
        {"value": 10},
        {"status": None},
        {"status": "pending"},
        {"position": -1},
        {"to": {"hash": "0xzz"}},
    ],
)
async def test_a_malformed_row_refuses_the_read(bad) -> None:
    row = item(1, 4_000) | bad
    src, _ = source([page([row])])
    result = await read(src)
    assert result.status is Availability.UNAVAILABLE
    assert result.failure is AtlasSourceFailure.INVALID_RESPONSE
    assert result.transactions == ()


async def test_an_order_against_the_contract_is_refused() -> None:
    src, _ = source([page([item(1, 3_000), item(2, 4_000)])])
    assert (await read(src)).failure is AtlasSourceFailure.INVALID_RESPONSE


async def test_a_repeating_cursor_is_refused() -> None:
    src, _ = source([page([item(3, 4_900)], cursor(4_900)), page([item(2, 4_800)], cursor(4_900))])
    result = await read(src)
    # The second page repeats the first cursor: a loop, never a complete answer.
    assert result.status is Availability.UNAVAILABLE


async def test_a_cursor_with_unknown_keys_is_refused() -> None:
    src, _ = source([page([item(3, 4_900)], {"block_number": 1, "redirect": "x"})])
    assert (await read(src)).failure is AtlasSourceFailure.INVALID_RESPONSE


def live_cursor(block: int, index: int = 0, **overrides: object) -> dict[str, object]:
    """The cursor shape Blockscout PRO actually returns for ``filter=from``.

    Synthetic values; the key set and every value type are those of the live
    response: the documented ``filter`` query parameter is echoed back beside
    the keyset keys and ``items_count``.
    """
    return {
        "block_number": block,
        "fee": "21000000000000",
        "filter": "from",
        "hash": "0x" + "ab" * 32,
        "index": index,
        "inserted_at": "2025-01-01T00:00:00.000000Z",
        "items_count": 50,
        "value": "1000000000000000",
    } | overrides


async def test_the_live_cursor_shape_with_its_echoed_filter_is_followed() -> None:
    later = [item(100 + i, SNAPSHOT + 500 - i, position=0) for i in range(3)]
    src, routes = source(
        [
            page(later, live_cursor(SNAPSHOT + 498)),  # all after the snapshot
            page([item(2, 4_000), item(1, 900)]),  # 900 < creation
        ]
    )
    result = await read(src)
    assert result.status is Availability.AVAILABLE
    assert result.coverage is FundingCoverage.COMPLETE
    assert [tx.block_number for tx in result.transactions] == [4_000]
    assert len(routes.requests) == 2
    follow = routes.requests[1].url.params
    assert follow.get_list("filter") == ["from"]
    assert follow["block_number"] == str(SNAPSHOT + 498)
    assert follow["inserted_at"] == "2025-01-01T00:00:00.000000Z"


@pytest.mark.parametrize("echoed", ["to", "", "FROM", 1, True])
async def test_an_echoed_filter_other_than_ours_is_refused(echoed) -> None:
    src, routes = source([page([item(3, 4_900)], live_cursor(4_900, filter=echoed))])
    assert (await read(src)).failure is AtlasSourceFailure.INVALID_RESPONSE
    assert len(routes.requests) == 1


@pytest.mark.parametrize("extra", [{"sort": "block_number"}, {"order": "asc"}, {"apikey": "x"}])
async def test_a_live_cursor_with_an_undocumented_cursor_key_is_refused(extra) -> None:
    src, routes = source([page([item(3, 4_900)], live_cursor(4_900) | extra)])
    assert (await read(src)).failure is AtlasSourceFailure.INVALID_RESPONSE
    assert len(routes.requests) == 1


async def test_an_empty_page_promising_more_is_refused() -> None:
    src, _ = source([page([], cursor(4_000))])
    assert (await read(src)).failure is AtlasSourceFailure.INVALID_RESPONSE


@pytest.mark.parametrize(
    ("status", "failure"),
    [
        (401, AtlasSourceFailure.NOT_CONFIGURED),
        (403, AtlasSourceFailure.NOT_CONFIGURED),
        (429, AtlasSourceFailure.RATE_LIMIT),
        (503, AtlasSourceFailure.UNAVAILABLE),
    ],
)
async def test_provider_failures_are_safe_categories(status, failure) -> None:
    src, _ = source([{"message": "nope"}], status=status)
    result = await read(src)
    assert result.status is Availability.UNAVAILABLE
    assert result.failure is failure


async def test_another_chain_is_refused_without_a_request() -> None:
    src, routes = source([])
    result = await read(src, chain="bsc")
    assert result.failure is AtlasSourceFailure.UNSUPPORTED_CHAIN
    assert not routes.requests


def test_the_page_budget_is_hard_capped() -> None:
    with pytest.raises(ValueError):
        BlockscoutFundingConfig(
            base_url="https://api.blockscout.test",
            chain_id=4663,
            api_key="k",
            max_pages=MAX_FUNDING_PAGES + 1,
        )


# A timestamp only V2 history depends on, broken in every way it can be.
BAD_TIMES = {
    "missing": "drop",
    "null": None,
    "malformed": "yesterday",
    "naive": "2026-01-01T00:00:00",
    "epoch_int": 1_767_225_600,
}


def with_time(row: dict, bad: object) -> dict:
    if bad == "drop":
        return {key: value for key, value in row.items() if key != "timestamp"}
    return row | {"timestamp": bad}


def pre_v2_row(index: int, block: int, **kw) -> dict:
    """A row exactly as V1 read it before V2: no timestamp field at all."""
    return with_time(item(index, block, **kw), "drop")


@pytest.mark.parametrize("bad", list(BAD_TIMES.values()), ids=list(BAD_TIMES))
async def test_a_v1_only_read_never_depends_on_timestamps(bad) -> None:
    rows = [with_time(item(2, 4_500), bad), with_time(item(1, 1_200), bad)]
    src, routes = source([page(rows)])
    result = await read(src)
    assert result.status is Availability.AVAILABLE
    assert result.coverage is FundingCoverage.COMPLETE
    assert [tx.block_number for tx in result.transactions] == [4_500, 1_200]
    assert all(tx.observed_at is None for tx in result.transactions)
    assert result.history_failure is None and len(routes.requests) == 1


async def test_a_v1_only_read_ignores_timestamps_running_backwards() -> None:
    rows = [item(2, 4_500, timestamp=stamp(1_000)), item(1, 4_400, timestamp=stamp(4_000))]
    result = await read(source([page(rows)])[0])
    assert result.status is Availability.AVAILABLE
    assert [tx.block_number for tx in result.transactions] == [4_500, 4_400]


async def test_a_pre_v2_v1_read_yields_exactly_the_pre_v2_facts() -> None:
    from src.agents.atlas.funding.models import FundingTransaction

    rows = [
        pre_v2_row(3, 4_000, status="error"),
        pre_v2_row(2, 3_900, to=None),
        pre_v2_row(1, 3_800, value="0"),
        pre_v2_row(0, CREATED - 1),
    ]
    result = await read(source([page(rows, cursor(CREATED - 1))])[0])
    assert result.status is Availability.AVAILABLE
    assert result.coverage is FundingCoverage.COMPLETE
    assert result.transactions == (
        FundingTransaction(
            tx_hash="0x" + format(3, "064x"),
            block_number=4_000,
            sender=CREATOR,
            recipient="0x" + format(3, "040x"),
            native_value_raw=10**15,
            succeeded=False,
        ),
        FundingTransaction(
            tx_hash="0x" + format(2, "064x"),
            block_number=3_900,
            sender=CREATOR,
            recipient=None,
            native_value_raw=10**15,
            succeeded=True,
        ),
        FundingTransaction(
            tx_hash="0x" + format(1, "064x"),
            block_number=3_800,
            sender=CREATOR,
            recipient="0x" + format(1, "040x"),
            native_value_raw=0,
            succeeded=True,
        ),
    )
    assert result.prelaunch_transactions == () and result.history_until is None


async def test_rows_carry_their_block_time_in_utc_when_history_is_read() -> None:
    offset = (at(4_000) + timedelta(hours=2)).isoformat().replace("+00:00", "+02:00")
    src, _ = source([page([item(1, 4_000, timestamp=offset)])])
    (row,) = (await read(src, history_until=at(CREATED - 100))).transactions
    assert row.observed_at == at(4_000) and row.observed_at.tzinfo is UTC


@pytest.mark.parametrize("bad", list(BAD_TIMES.values()), ids=list(BAD_TIMES))
async def test_a_bad_timestamp_fails_only_the_history(bad) -> None:
    rows = [item(3, 4_500), with_time(item(2, CREATED - 1), bad), item(1, CREATED - 2)]
    src, routes = source(
        [page(rows, cursor(CREATED - 2)), page([item(0, CREATED - 500)])]  # 2nd never read
    )
    result = await read(src, history_until=at(CREATED - 1_000))
    # V1 answered from the same page, exactly where a V1-only read stops.
    assert result.status is Availability.AVAILABLE
    assert result.coverage is FundingCoverage.COMPLETE
    assert [tx.block_number for tx in result.transactions] == [4_500]
    assert len(routes.requests) == 1
    # The history is unusable and says so; it carries nothing.
    assert result.history_failure is AtlasSourceFailure.INVALID_RESPONSE
    assert result.prelaunch_transactions == ()
    assert result.history_ended is False and result.oldest_observed_at is None


async def test_time_running_backwards_fails_only_the_history() -> None:
    rows = [item(2, 4_000, timestamp=stamp(3_000)), item(1, 3_999, timestamp=stamp(3_500))]
    result = await read(source([page(rows)])[0], history_until=at(CREATED - 100))
    assert result.status is Availability.AVAILABLE
    assert [tx.block_number for tx in result.transactions] == [4_000, 3_999]
    assert result.history_failure is AtlasSourceFailure.INVALID_RESPONSE
    assert result.history_ended is False


async def test_a_v1_fault_still_fails_the_whole_read_in_history_mode() -> None:
    rows = [item(1, 4_000, sender="0x" + "12" * 20)]
    result = await read(source([page(rows)])[0], history_until=at(CREATED - 100))
    assert result.status is Availability.UNAVAILABLE
    assert result.failure is AtlasSourceFailure.INVALID_RESPONSE


# ------------------------------------------------------- V2: one read, history


async def test_without_history_the_read_stops_at_creation_as_before() -> None:
    src, routes = source(
        [
            page([item(3, 4_900), item(2, CREATED - 1)], cursor(CREATED - 1)),
            page([item(1, CREATED - 50)]),
        ]
    )
    result = await read(src)
    assert result.coverage is FundingCoverage.COMPLETE
    assert len(routes.requests) == 1
    assert result.prelaunch_transactions == () and result.history_until is None


async def test_history_continues_past_creation_until_a_row_older_than_the_cutoff() -> None:
    until = at(CREATED - 120)
    src, routes = source(
        [
            page([item(5, 4_900), item(4, CREATED - 10)], cursor(CREATED - 10)),
            page([item(3, CREATED - 60), item(2, CREATED - 120)], cursor(CREATED - 120)),
            page([item(1, CREATED - 121)], cursor(CREATED - 121)),  # older than the cutoff
            page([item(0, CREATED - 500)]),  # never requested
        ]
    )
    result = await read(src, history_until=until)
    assert len(routes.requests) == 3
    assert result.coverage is FundingCoverage.COMPLETE  # V1 reached creation
    assert [tx.block_number for tx in result.transactions] == [4_900]
    # Before creation, no older than the cutoff; the proving row itself is not kept.
    assert [tx.block_number for tx in result.prelaunch_transactions] == [
        CREATED - 10,
        CREATED - 60,
        CREATED - 120,
    ]
    assert result.history_ended is False
    assert result.oldest_observed_at == at(CREATED - 121) < until
    assert result.history_until == until


async def test_a_history_that_ends_is_complete_without_reaching_the_cutoff() -> None:
    src, _ = source([page([item(2, 4_900), item(1, CREATED - 5)])])
    result = await read(src, history_until=at(CREATED - 1_000))
    assert result.history_ended is True
    assert result.coverage is FundingCoverage.COMPLETE
    assert [tx.block_number for tx in result.prelaunch_transactions] == [CREATED - 5]


async def test_a_budget_cut_history_says_how_far_it_reached() -> None:
    pages = [
        page([item(10 - n, CREATED - 10 * n)], cursor(CREATED - 10 * n)) for n in range(3)
    ]  # CONFIG allows three pages
    src, routes = source(pages)
    result = await read(src, history_until=at(CREATED - 1_000))
    assert len(routes.requests) == 3
    assert result.history_ended is False
    assert result.coverage is FundingCoverage.COMPLETE  # creation itself was reached
    assert result.oldest_observed_at == at(CREATED - 20)


async def test_a_naive_history_bound_is_refused_without_a_request() -> None:
    src, routes = source([])
    result = await read(src, history_until=datetime(2026, 1, 1))  # noqa: DTZ001
    assert result.failure is AtlasSourceFailure.INVALID_RESPONSE
    assert not routes.requests
