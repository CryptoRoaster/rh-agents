"""One textual form per `Decimal` value, with no arithmetic anywhere in it.

`src.core.numbers.canonical_decimal` normalises inside a context of precision
seventy-eight. That is enough for every amount it was written for, and it is not
enough here: a recorded market price may carry up to a hundred coefficient
digits, and normalising one *rounds* it. Two prices that differ in the
ninetieth digit — and that produce two genuinely different quantities — then
canonicalise to the same string and hash to the same digest.

So this formatter does the same job losslessly, and deliberately does it without
calling `normalize`, `quantize` or any operator on the value. It reads the
value's own sign, digits and exponent, strips trailing zeros as a change of
*notation*, and writes the result out in plain positional form. Nothing consults
the arithmetic context, so the answer cannot depend on what precision some
caller happened to have set.

It is kept here rather than replacing the shared helper. That helper feeds the
digests inside stored specialist evidence, and evidence is append-only with its
fingerprints recorded — changing how any of it serialises is a compatibility
question of its own, not a side effect of fixing sizing.
"""

from decimal import Decimal


def lossless_decimal(value: Decimal) -> str:
    """The canonical text of an exact decimal value.

    Equal values written differently give one string: `500`, `500.00` and
    `5E+2` all become `"500"`. Unequal values never collide, at any magnitude
    or precision the market layer permits, because no digit is ever discarded.
    """
    if not value.is_finite():
        raise ValueError("Only finite decimal values have a canonical form")
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):  # pragma: no cover - guarded by is_finite
        raise ValueError("Only finite decimal values have a canonical form")

    # Trailing zeros are notation, not value, so they go — one at a time and by
    # moving the exponent, which cannot lose a significant digit the way a
    # context-bound normalisation can.
    coefficient = list(digits)
    while len(coefficient) > 1 and coefficient[-1] == 0:
        coefficient.pop()
        exponent += 1
    if coefficient == [0]:
        # Zero has one form and no sign. `-0` and `0.00` are the same amount.
        return "0"

    # Named `written` rather than `text`: `text` is SQLAlchemy's raw-SQL escape
    # hatch, and a test asserts that identifier never appears in this package.
    written = "".join(str(digit) for digit in coefficient)
    if exponent >= 0:
        whole, fraction = written + "0" * exponent, ""
    else:
        point = len(written) + exponent
        if point > 0:
            whole, fraction = written[:point], written[point:]
        else:
            whole, fraction = "0", "0" * -point + written
    return f"{'-' if sign else ''}{whole}" + (f".{fraction}" if fraction else "")
