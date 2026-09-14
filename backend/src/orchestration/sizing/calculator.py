"""The arithmetic, as a pure function of typed inputs.

No clock, no session, no provider and no I/O: `now` arrives as an argument, every
fact arrives already read, and the same inputs always produce the same answer.
That is what makes replay checkable rather than merely likely.

Everything here is `Decimal`. Not one float enters the computation or the
identity — a binary fraction cannot represent a tenth, and a sizing error of one
part in 10^16 is still a sizing error nobody can explain afterwards.
"""

from collections.abc import Callable
from datetime import datetime
from decimal import ROUND_DOWN, Decimal, localcontext
from uuid import UUID

from src.core.models import Side, TradingMode
from src.orchestration.sizing.models import (
    BaseAssetMetadata,
    ReferencePrice,
    SizingAssessment,
    SizingReading,
    SizingRefusal,
    SizingRefused,
    sizing_input_digest,
)
from src.orchestration.sizing.policy import PaperSizingPolicy

# Working precision for the division and for the exact product that checks it.
#
# A recorded price may carry up to a hundred coefficient digits and a ledger
# quantity up to thirty-eight, so their product needs at most a hundred and
# thirty-eight to be exact. Three hundred leaves the multiplication provably
# unrounded with room to spare, and the division — which rarely terminates — is
# truncated deliberately afterwards rather than relied upon.
EXACT_PRECISION = 300

# The finest amount the ledger can hold, used to floor the reference notional.
LEDGER_UNIT = Decimal(1).scaleb(-18)


def assess_paper_sizing(
    *,
    trade_case_id: UUID,
    base_asset_id: str,
    setup_evidence_id: UUID,
    side: Side,
    trading_mode: TradingMode,
    requested_notional_usd: Decimal | None,
    price: ReferencePrice | None,
    base_asset: BaseAssetMetadata | None,
    now: datetime,
    policy: PaperSizingPolicy,
) -> SizingReading:
    """How many base tokens the configured amount buys, or why it buys none.

    Checks run from the widest fact to the narrowest. A deployment that does not
    act at all is reported as such before a missing configuration, and a missing
    configuration before anything about the market — otherwise a stopped system
    with no configured amount would report the knob rather than the stop, and an
    operator would fix the wrong thing.
    """
    refused = _refusal(trade_case_id, base_asset_id, policy)

    if trading_mode not in policy.supported_modes:
        return refused(SizingRefusal.SIZING_MODE_NOT_SUPPORTED)
    if side not in policy.supported_sides:
        return refused(SizingRefusal.SIZING_SIDE_NOT_SUPPORTED)
    if requested_notional_usd is None:
        # The standing gap. Nothing is guessed, derived from equity, or taken
        # from a maximum that happens to be lying around.
        return refused(SizingRefusal.AUTONOMOUS_SIZING_INPUT_MISSING)
    if not requested_notional_usd.is_finite() or requested_notional_usd <= 0:
        # Defensive: configuration validation refuses these at boot. A caller
        # reaching this function another way must not get a size out of it.
        # Finiteness is tested first because comparing a NaN raises rather than
        # answering, and an exception here would be a crash instead of a stop.
        return refused(SizingRefusal.AUTONOMOUS_SIZING_INPUT_MISSING)

    if price is None:
        return refused(SizingRefusal.SIZING_PRICE_UNAVAILABLE)
    if price.asset_id != base_asset_id:
        return refused(SizingRefusal.SIZING_PRICE_ASSET_MISMATCH)
    if price.observed_at > now:
        # A source that reports the future is not merely stale. Sizing against
        # it would mean trusting a recorder about when anything happened.
        return refused(SizingRefusal.SIZING_PRICE_NOT_YET_OBSERVED)
    if now - price.observed_at > policy.max_price_age:
        return refused(SizingRefusal.SIZING_PRICE_STALE)

    if base_asset is None:
        return refused(SizingRefusal.SIZING_TOKEN_METADATA_MISSING)
    if base_asset.asset_id != base_asset_id:
        return refused(SizingRefusal.SIZING_TOKEN_METADATA_MISSING)

    unit = policy.supported_unit(base_asset.decimals)
    exponent = unit.as_tuple().exponent
    assert isinstance(exponent, int)
    places = -exponent

    with localcontext() as arithmetic:
        arithmetic.prec = EXACT_PRECISION
        exact = requested_notional_usd / price.usd_per_base_unit
    if exact.adjusted() + 1 > policy.max_integer_digits:
        # The amount buys more tokens than the ledger can hold. Checked on the
        # unrounded quotient, before any attempt to express it in units: a
        # cheap enough token makes that figure large enough that rounding it is
        # not merely wrong but arithmetically impossible at any sane precision.
        # Truncating the magnitude to fit would silently size something else.
        return refused(SizingRefusal.SIZING_QUANTITY_NOT_REPRESENTABLE)

    quantity = _floor_quantity(requested_notional_usd, price.usd_per_base_unit, unit)
    if quantity <= 0:
        return refused(SizingRefusal.SIZING_BELOW_MINIMUM_UNIT)

    with localcontext() as arithmetic:
        arithmetic.prec = EXACT_PRECISION
        reference_notional = (quantity * price.usd_per_base_unit).quantize(
            LEDGER_UNIT, rounding=ROUND_DOWN
        )
    if reference_notional <= 0:
        # The quantity is representable but worth less than the smallest amount
        # the ledger can express, so there is no honest notional to report.
        return refused(SizingRefusal.SIZING_BELOW_MINIMUM_UNIT)

    return SizingAssessment(
        policy_version=policy.version,
        trade_case_id=trade_case_id,
        base_asset_id=base_asset_id,
        setup_evidence_id=setup_evidence_id,
        side=side,
        trading_mode=trading_mode,
        requested_notional_usd=requested_notional_usd,
        reference_price=price,
        base_asset=base_asset,
        quantity_decimal_places=places,
        quantity=quantity,
        reference_notional_usd=reference_notional,
        # Anchored to the observation, never to this call. Reading a source
        # again does not make it younger, and a validity that renewed itself on
        # every read would describe nothing.
        valid_until=price.observed_at + policy.max_price_age,
        input_digest=sizing_input_digest(
            policy_version=policy.version,
            trade_case_id=trade_case_id,
            base_asset_id=base_asset_id,
            setup_evidence_id=setup_evidence_id,
            side=side,
            trading_mode=trading_mode,
            requested_notional_usd=requested_notional_usd,
            price=price,
            base_asset=base_asset,
            quantity_decimal_places=places,
        ),
    )


def _floor_quantity(notional: Decimal, price: Decimal, unit: Decimal) -> Decimal:
    """The largest whole number of units whose value stays within the notional.

    Rounded down, always. Rounding up would ask for more than was configured,
    and doing so by a single unit is still asking for something nobody approved.

    The truncated division is then *verified* rather than trusted. Dividing at
    finite precision can round the quotient upward at the last digit it keeps,
    and if that carry crossed a unit boundary the truncated result would exceed
    the notional by one unit. Checking the exact product and stepping down costs
    nothing and turns "overwhelmingly unlikely" into "cannot happen", the same
    way the risk engine guards its own conservative capacity figure.
    """
    with localcontext() as arithmetic:
        arithmetic.prec = EXACT_PRECISION
        quantity = (notional / price).quantize(unit, rounding=ROUND_DOWN)
        while quantity > 0 and quantity * price > notional:
            quantity -= unit
        return quantity


def _refusal(
    trade_case_id: UUID, base_asset_id: str | None, policy: PaperSizingPolicy
) -> Callable[[SizingRefusal], SizingRefused]:
    def build(reason: SizingRefusal) -> SizingRefused:
        return SizingRefused(
            reason=reason,
            policy_version=policy.version,
            trade_case_id=trade_case_id,
            base_asset_id=base_asset_id,
        )

    return build
