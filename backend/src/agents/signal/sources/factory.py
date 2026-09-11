"""Construct the configured SIGNAL social source. Construction only — nothing starts.

Building a source is not running one. No worker is launched here, no request is
made, and a deployment left at ``disabled`` gets no source at all rather than a
quietly optional one.

There is deliberately no fallback to the deterministic fake. If the provider
cannot answer, SIGNAL is unavailable and the case waits — synthetic sentiment
carrying a real TradeCase forward is the one failure mode a fixture source could
cause, and the way to prevent it is for production to have no path to one.
"""

from src.agents.signal.ports import SignalObservationReadPort
from src.agents.signal.sources.neynar import NeynarConfig, NeynarSignalSource
from src.core.clock import Clock, SystemClock
from src.core.config import Settings


def social_source(
    settings: Settings, *, clock: Clock | None = None
) -> SignalObservationReadPort | None:
    """The configured social source, or nothing at all.

    ``None`` is a real answer and the default one: it leaves the SIGNAL context
    reader without a source, which surfaces as an unavailable domain rather than
    as an empty feed. A key sitting in the environment selects nothing.
    """
    if settings.signal_social_provider != "neynar":
        return None
    return NeynarSignalSource(
        config=NeynarConfig(
            base_url=settings.neynar_base_url,
            api_key=settings.neynar_api_key.get_secret_value(),
            timeout_seconds=settings.signal_source_timeout_seconds,
            max_pages=settings.signal_neynar_max_pages,
            page_size=settings.signal_neynar_page_size,
        ),
        clock=clock if clock is not None else SystemClock(),
    )
