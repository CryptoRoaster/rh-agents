"""V4 protocol arithmetic against independent, published reference values.

The pool ids are the two real Robinhood Chain pools of the historical REVENUE
case, as the PoolManager emitted them: they prove the PoolKey hash and the
Keccak implementation together. The tick vectors are TickMath's documented
boundaries. No RPC is involved.
"""

import pytest

from src.agents.atlas.v4.keccak import keccak256, selector
from src.agents.atlas.v4.math import (
    MAX_SQRT_PRICE,
    MAX_TICK,
    MIN_SQRT_PRICE,
    MIN_TICK,
    Q96,
    LiquidityMathError,
    position_amounts,
    sqrt_price_at_tick,
)
from src.agents.atlas.v4.protocol import (
    INITIALIZE_TOPIC,
    MODIFY_LIQUIDITY_TOPIC,
    V4DecodeError,
    decode_address_result,
    decode_initialize,
    decode_modify_liquidity,
    decode_pool_and_position_info,
    decode_slot0,
    hook_permissions,
    pool_id,
    word,
)

HISTORICAL_TOKEN = "0x645de8815be6972d971d07638273382fb1d1636b"
NATIVE = "0x" + "0" * 40


def test_keccak_is_ethereum_keccak_not_nist_sha3() -> None:
    assert (
        keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    )
    # More than one 136-byte block.
    assert keccak256(b"x" * 200) != keccak256(b"x" * 199)


def test_event_topics_and_selectors_match_the_protocol() -> None:
    assert INITIALIZE_TOPIC == "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
    assert (
        MODIFY_LIQUIDITY_TOPIC
        == "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec"
    )
    assert selector("owner()") == "0x8da5cb5b"
    assert selector("ownerOf(uint256)") == "0x6352211e"
    assert selector("extsload(bytes32)") == "0x1e2eaeaf"
    assert selector("poolManager()") == "0xdc4c90d3"
    assert selector("getPoolAndPositionInfo(uint256)") == "0x7ba03aad"


def test_pool_ids_hash_from_their_pool_keys_as_on_chain() -> None:
    traded = pool_id(NATIVE, HISTORICAL_TOKEN, 2500, 25, NATIVE)
    hidden = pool_id(NATIVE, HISTORICAL_TOKEN, 100, 1, "0x5dfb76dd14d7817f5d62f7b9bf862a125805e080")
    assert traded == "0xb7557b36948caf2322bde92ccfd26336c94ba5d448955d411438df4990ed8f80"
    assert hidden == "0x9e337cc7cb7a3b8292f9c37c09902c24817defaaa806bea3f584593bb677a70e"


def test_hook_permissions_come_from_the_address_bits() -> None:
    assert hook_permissions("0x5dfb76dd14d7817f5d62f7b9bf862a125805e080") == (
        "BEFORE_INITIALIZE",
        "BEFORE_SWAP",
    )
    assert hook_permissions(NATIVE) == ()
    assert len(hook_permissions("0x" + "f" * 40)) == 14


def test_tick_math_boundaries() -> None:
    assert sqrt_price_at_tick(MIN_TICK) == MIN_SQRT_PRICE
    assert sqrt_price_at_tick(MAX_TICK) == MAX_SQRT_PRICE
    assert sqrt_price_at_tick(0) == Q96
    assert sqrt_price_at_tick(1) == 79232123823359799118286999568
    assert sqrt_price_at_tick(-1) == 79224201403219477170569942574
    with pytest.raises(LiquidityMathError):
        sqrt_price_at_tick(MAX_TICK + 1)


def test_the_historical_single_sided_position_holds_574_million_tokens() -> None:
    """Range and liquidity of the hidden position, priced above its range."""
    amount0, amount1 = position_amounts(
        sqrt_price_at_tick(300_000), -887272, 299351, 181514997636243302190
    )
    assert amount0 == 0
    assert amount1 // 10**18 == 573_999_999
    assert 574_000_000 * 10**18 - amount1 < 10**10


def test_positions_below_inside_and_above_their_range() -> None:
    liquidity = 10**24
    below = position_amounts(sqrt_price_at_tick(-100), -50, 50, liquidity)
    inside = position_amounts(sqrt_price_at_tick(0), -50, 50, liquidity)
    above = position_amounts(sqrt_price_at_tick(100), -50, 50, liquidity)
    assert below[0] > 0 and below[1] == 0
    assert inside[0] > 0 and inside[1] > 0
    assert above[0] == 0 and above[1] > 0
    # Exactly at the lower bound the position is still entirely token0.
    assert position_amounts(sqrt_price_at_tick(-50), -50, 50, liquidity)[1] == 0
    assert position_amounts(sqrt_price_at_tick(0), -50, 50, 0) == (0, 0)


def test_amounts_are_integers_rounded_down() -> None:
    amount0, amount1 = position_amounts(sqrt_price_at_tick(7), -60, 60, 123_456_789)
    assert type(amount0) is int and type(amount1) is int


@pytest.mark.parametrize("lower, upper", [(10, 10), (20, 10)])
def test_an_inverted_range_is_refused(lower, upper) -> None:
    with pytest.raises(LiquidityMathError):
        position_amounts(Q96, lower, upper, 1)


def encoded(*values: int) -> str:
    return "0x" + "".join(word(value).hex() for value in values)


def test_decoders_refuse_dirty_or_short_words() -> None:
    with pytest.raises(V4DecodeError):
        decode_address_result(encoded(1 << 200))
    with pytest.raises(V4DecodeError):
        decode_address_result("0x" + "00" * 31)
    with pytest.raises(V4DecodeError):
        decode_slot0("0x1234")
    topics = (INITIALIZE_TOPIC, "0x" + "11" * 32, "0x" + "00" * 32, "0x" + "00" * 32)
    # A fee wider than uint24 is not an Initialize event.
    with pytest.raises(V4DecodeError):
        decode_initialize(topics, encoded(1 << 24, 1, 0, Q96, 0), 1)
    with pytest.raises(V4DecodeError):
        decode_modify_liquidity(topics[:3], encoded(1 << 30, 0, 0, 0), 1)


def test_signed_fields_decode_from_twos_complement() -> None:
    topics = (MODIFY_LIQUIDITY_TOPIC, "0x" + "11" * 32, "0x" + "00" * 12 + "ab" * 20)
    event = decode_modify_liquidity(topics, encoded(-887272, 299351, -5, 7), 9)
    assert (event.tick_lower, event.tick_upper, event.liquidity_delta) == (-887272, 299351, -5)
    assert event.sender == "0x" + "ab" * 20
    sqrt, tick = decode_slot0(encoded(Q96 | ((-3 % (1 << 24)) << 160)))
    assert (sqrt, tick) == (Q96, -3)


def test_position_info_binds_a_token_id_to_its_pool_and_range() -> None:
    key = (NATIVE, HISTORICAL_TOKEN, 100, 1, "0x5dfb76dd14d7817f5d62f7b9bf862a125805e080")
    pid = pool_id(*key)
    info = (int(pid, 16) >> 56 << 56) | ((299351 % (1 << 24)) << 32) | ((-887272 % (1 << 24)) << 8)
    decoded = decode_pool_and_position_info(
        encoded(0, int(HISTORICAL_TOKEN, 16), 100, 1, int(key[4], 16), info)
    )
    assert decoded.pool_id == pid
    assert (decoded.tick_lower, decoded.tick_upper) == (-887272, 299351)
    assert decoded.truncated_pool_id == int(pid, 16) >> 56
