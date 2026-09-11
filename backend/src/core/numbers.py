from decimal import Decimal, localcontext


def quantize(value: Decimal) -> Decimal:
    """Use the ledger's 18-place storage precision at accounting boundaries."""
    with localcontext() as context:
        context.prec = 78
        return value.quantize(Decimal("0.000000000000000001"))


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
