"""The versioned PAPER entry-sizing policy.

One question is answered here: *how large an entry did the operator ask for?*
It is deliberately the smallest possible answer — a fixed USD amount somebody
configured — because every larger answer is a trading strategy, and a strategy
that arrived as a default nobody chose is the worst kind.

There is no portfolio fraction, no volatility target, no Kelly criterion and no
scaling rule. Those decide how much money to put at risk, which is a decision a
person makes and a system executes, not the reverse. Adding one here would also
make the amount depend on state that moves, so the same case would ask for a
different size on every read.

Nothing in this module is an authorisation. A configured size says what was
asked for; whether it may happen is SENTINEL's question, asked later, with
inputs this policy cannot see.
"""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from src.core.models import Side, TradingMode

# The ledger stores quantities as ``Numeric(38, 18)`` and every core amount is
# declared ``max_digits=38, decimal_places=18``. A derived quantity that cannot
# be represented there is refused rather than silently truncated to fit, because
# a quantity that does not survive storage is not the quantity that was computed.
LEDGER_DECIMAL_PLACES = 18
LEDGER_TOTAL_DIGITS = 38


@dataclass(frozen=True)
class PaperSizingPolicy:
    """Deterministic bounds on turning a configured USD amount into a quantity."""

    version: str
    # How old the reference price may be. Ninety seconds, matching
    # `ANCHOR_EXECUTION_V1.max_reference_age`, because it is the same fact from
    # the same recorder: the market layer writes observations on a cadence of
    # roughly that length, so a tighter bound would refuse the freshest reading
    # that exists and a looser one would size against a market that has moved.
    max_price_age: timedelta
    # Long-only entries, because that is what the rest of the system can do:
    # VECTOR proposes `Side.BUY` exclusively and the paper ledger is long-only
    # weighted-average-cost. A SELL entry here would describe something no other
    # component could carry out.
    supported_sides: frozenset[Side]
    # PAPER only. OBSERVE means the deployment watches and does not act, and
    # LIVE_AUTONOMOUS is not implemented anywhere in this system.
    supported_modes: frozenset[TradingMode]
    # The representable quantity envelope, carried as policy so the refusal that
    # enforces it names a rule rather than an implementation detail.
    max_quantity_decimal_places: int
    max_quantity_total_digits: int

    def __post_init__(self) -> None:
        if self.max_price_age <= timedelta(0):
            raise ValueError("Price freshness tolerance must be positive")
        if not self.supported_sides:
            raise ValueError("A sizing policy must support at least one side")
        if not self.supported_modes:
            raise ValueError("A sizing policy must support at least one trading mode")
        if TradingMode.LIVE_AUTONOMOUS in self.supported_modes:
            # Stated as a property of the policy rather than as a runtime branch
            # somebody could forget: live execution does not exist here.
            raise ValueError("Live execution is unavailable")
        if not 0 <= self.max_quantity_decimal_places <= LEDGER_DECIMAL_PLACES:
            raise ValueError("Quantity precision cannot exceed the ledger's own")
        if not 1 <= self.max_quantity_total_digits <= LEDGER_TOTAL_DIGITS:
            raise ValueError("Quantity magnitude cannot exceed the ledger's own")

    @property
    def max_integer_digits(self) -> int:
        """How many digits a quantity may carry left of the decimal point."""
        return self.max_quantity_total_digits - self.max_quantity_decimal_places

    def supported_unit(self, base_decimals: int) -> Decimal:
        """The smallest quantity this system can represent for that token.

        A token may define more decimal places than the ledger can store — the
        recorded metadata allows up to thirty-six — so the unit actually used is
        the coarser of the two. It is returned rather than assumed so the
        assessment can record both figures side by side: a quantity rounded to
        eighteen places for a twenty-four-decimal token is rounded correctly and
        still *not* to that token's own smallest unit, and a reader deserves to
        see which of the two bound the result.
        """
        if base_decimals < 0:
            raise ValueError("Token decimals cannot be negative")
        return Decimal(1).scaleb(-min(base_decimals, self.max_quantity_decimal_places))


PAPER_SIZING_V1 = PaperSizingPolicy(
    version="paper-sizing-v1",
    max_price_age=timedelta(seconds=90),
    supported_sides=frozenset({Side.BUY}),
    supported_modes=frozenset({TradingMode.PAPER}),
    max_quantity_decimal_places=LEDGER_DECIMAL_PLACES,
    max_quantity_total_digits=LEDGER_TOTAL_DIGITS,
)
