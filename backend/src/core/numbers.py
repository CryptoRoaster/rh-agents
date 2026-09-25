from decimal import ROUND_CEILING, ROUND_DOWN, Decimal, localcontext


def quantize(value: Decimal) -> Decimal:
    """Use the ledger's 18-place storage precision at accounting boundaries."""
    with localcontext() as context:
        context.prec = 78
        return value.quantize(Decimal("0.000000000000000001"))


# `Numeric(38, 18)`: eighteen places leave twenty integer digits.
LEDGER_UNIT = Decimal("0.000000000000000001")
LEDGER_INTEGER_DIGITS = 20


def fits_ledger(value: Decimal) -> bool:
    """Whether a finite value's integer part fits `Numeric(38, 18)`.

    Checked before and after rounding to the ledger's scale: before, because
    quantizing a far larger value is not possible at any working precision;
    after, because rounding up can carry into one more integer digit.
    """
    return value.is_finite() and (value.is_zero() or value.adjusted() < LEDGER_INTEGER_DIGITS)


def quantize_up(value: Decimal) -> Decimal:
    """The ledger's 18 places, rounded toward positive infinity.

    For an accounting boundary where rounding down would flatter the value,
    such as the price a buy is judged at: a ceiling can only overstate what the
    quantity costs, and never by as much as one ledger unit.
    """
    with localcontext() as context:
        context.prec = 78
        return value.quantize(LEDGER_UNIT, rounding=ROUND_CEILING)


def quantize_down(value: Decimal) -> Decimal:
    """The ledger's 18 places, truncated toward zero instead of rounded.

    For an accounting boundary where rounding up would flatter the value, such
    as liquidity checked against a minimum: a floor can only understate it.
    """
    with localcontext() as context:
        context.prec = 78
        return value.quantize(Decimal("0.000000000000000001"), rounding=ROUND_DOWN)


def canonical_decimal(value: Decimal) -> str:
    """One unambiguous textual form for a Decimal.

    ``str(Decimal)`` yields scientific notation for small magnitudes ("1E-18"),
    which is both harder to read and a needless ambiguity, and it distinguishes
    values that compare equal ("1.10" from "1.1"). Normalizing first and
    formatting without an exponent gives equal values one identical string, so a
    fingerprint over serialized data is stable and no value ever passes through a
    float to get there.
    """
    with localcontext() as context:
        context.prec = 78
        return format(value.normalize(), "f")
