"""Uniswap V4 liquidity arithmetic, integer-exact.

Ports of the canonical ``TickMath.getSqrtPriceAtTick`` and the
``SqrtPriceMath`` amount deltas used by ``LiquidityAmounts``. Every value is a
Python ``int``; nothing passes through a float, so a position's token amount is
the same number the protocol itself would compute, rounded the same way
(down — what a position could withdraw, never more).
"""

MIN_TICK = -887272
MAX_TICK = 887272
MIN_SQRT_PRICE = 4295128739
MAX_SQRT_PRICE = 1461446703485210103287273052203988822378723970342
Q96 = 1 << 96
MAX_UINT128 = (1 << 128) - 1
MAX_UINT256 = (1 << 256) - 1

# (bit, multiplier) pairs, exactly as in TickMath.
_RATIO_STEPS = (
    (0x2, 0xFFF97272373D413259A46990580E213A),
    (0x4, 0xFFF2E50F5F656932EF12357CF3C7FDCC),
    (0x8, 0xFFE5CACA7E10E4E61C3624EAA0941CD0),
    (0x10, 0xFFCB9843D60F6159C9DB58835C926644),
    (0x20, 0xFF973B41FA98C081472E6896DFB254C0),
    (0x40, 0xFF2EA16466C96A3843EC78B326B52861),
    (0x80, 0xFE5DEE046A99A2A811C461F1969C3053),
    (0x100, 0xFCBE86C7900A88AEDCFFC83B479AA3A4),
    (0x200, 0xF987A7253AC413176F2B074CF7815E54),
    (0x400, 0xF3392B0822B70005940C7A398E4B70F3),
    (0x800, 0xE7159475A2C29B7443B29C7FA6E889D9),
    (0x1000, 0xD097F3BDFD2022B8845AD8F792AA5825),
    (0x2000, 0xA9F746462D870FDF8A65DC1F90E061E5),
    (0x4000, 0x70D869A156D2A1B890BB3DF62BAF32F7),
    (0x8000, 0x31BE135F97D08FD981231505542FCFA6),
    (0x10000, 0x9AA508B5B7A84E1C677DE54F3E99BC9),
    (0x20000, 0x5D6AF8DEDB81196699C329225EE604),
    (0x40000, 0x2216E584F5FA1EA926041BEDFE98),
    (0x80000, 0x48A170391F7DC42444E8FA2),
)


class LiquidityMathError(ValueError):
    """An input outside what the protocol itself could hold."""


def sqrt_price_at_tick(tick: int) -> int:
    if type(tick) is not int or not MIN_TICK <= tick <= MAX_TICK:
        raise LiquidityMathError("tick out of range")
    absolute = abs(tick)
    ratio = 0xFFFCB933BD6FAD37AA2D162D1A594001 if absolute & 0x1 else 1 << 128
    for bit, multiplier in _RATIO_STEPS:
        if absolute & bit:
            ratio = (ratio * multiplier) >> 128
    if tick > 0:
        ratio = MAX_UINT256 // ratio
    return (ratio >> 32) + (0 if ratio % (1 << 32) == 0 else 1)


def amount0_delta(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    """Token0 between two prices, rounded down (``SqrtPriceMath.getAmount0Delta``)."""
    lower, upper = sorted((sqrt_a, sqrt_b))
    if lower <= 0:
        raise LiquidityMathError("sqrt price must be positive")
    return ((liquidity << 96) * (upper - lower) // upper) // lower


def amount1_delta(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    """Token1 between two prices, rounded down (``SqrtPriceMath.getAmount1Delta``)."""
    lower, upper = sorted((sqrt_a, sqrt_b))
    return liquidity * (upper - lower) // Q96


def position_amounts(
    sqrt_price: int, tick_lower: int, tick_upper: int, liquidity: int
) -> tuple[int, int]:
    """(amount0, amount1) a position holds at ``sqrt_price``, rounded down.

    Below the range the position is entirely token0, above it entirely token1,
    and inside it holds both. A single-sided position is simply one whose range
    lies entirely on one side of the current price.
    """
    if not tick_lower < tick_upper:
        raise LiquidityMathError("tickLower must be below tickUpper")
    if not 0 <= liquidity <= MAX_UINT128:
        raise LiquidityMathError("liquidity out of range")
    if not MIN_SQRT_PRICE <= sqrt_price <= MAX_SQRT_PRICE:
        raise LiquidityMathError("sqrt price out of range")
    sqrt_lower = sqrt_price_at_tick(tick_lower)
    sqrt_upper = sqrt_price_at_tick(tick_upper)
    if sqrt_price <= sqrt_lower:
        return amount0_delta(sqrt_lower, sqrt_upper, liquidity), 0
    if sqrt_price < sqrt_upper:
        return (
            amount0_delta(sqrt_price, sqrt_upper, liquidity),
            amount1_delta(sqrt_lower, sqrt_price, liquidity),
        )
    return 0, amount1_delta(sqrt_lower, sqrt_upper, liquidity)
