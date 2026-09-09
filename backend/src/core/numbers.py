from decimal import Decimal, localcontext


def quantize(value: Decimal) -> Decimal:
    """Use the ledger's 18-place storage precision at accounting boundaries."""
    with localcontext() as context:
        context.prec = 78
        return value.quantize(Decimal("0.000000000000000001"))
