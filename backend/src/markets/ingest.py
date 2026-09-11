"""Explicit one-shot public market recording; no scheduler or trading actions."""

import argparse
import asyncio
from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from src.core.clock import Clock, SystemClock
from src.core.config import Settings
from src.data.database import connect
from src.markets.geckoterminal.adapter import GeckoTerminalAdapter
from src.markets.geckoterminal.errors import ConfigurationError, ProviderError
from src.markets.geckoterminal.networks import NetworkDirectory, selected_chains
from src.markets.geckoterminal.transport import GeckoTerminalTransport
from src.markets.recorder import MarketRecorder, record_pair


@dataclass
class IngestionSummary:
    chain: str
    discovered: int = 0
    recorded: int = 0
    failed: int = 0
    error: str | None = None
    readable: int = 0
    unavailable: int = 0
    rejected: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def safe_text(self) -> str:
        return (
            f"provider=geckoterminal chain={self.chain} discovered={self.discovered} "
            f"recorded={self.recorded} readable={self.readable} unavailable={self.unavailable} "
            f"rejected={self.rejected} failed={self.failed}"
            + " reasons="
            + (
                ",".join(f"{code}:{count}" for code, count in sorted(self.reasons.items()))
                or "none"
            )
            + (f" error={self.error}" if self.error else "")
        )


async def ingest_chain(adapter: GeckoTerminalAdapter, recorder: MarketRecorder) -> IngestionSummary:
    summary = IngestionSummary(adapter.chain.name)
    recorded = []
    try:
        for pair in await adapter.discover():
            recorded.append(await record_pair(adapter, pair, recorder))
            summary.recorded += 1
    except ProviderError as error:
        summary.error = error.code
        summary.failed += 1
    except (ValueError, SQLAlchemyError, OSError):
        summary.error = "recording_failed"
        summary.failed += 1
    finally:
        summary.discovered = adapter.discovered
        summary.rejected = adapter.failed
        summary.failed += summary.rejected
        summary.reasons = dict(adapter.rejections)
        if summary.error is not None:
            summary.reasons[summary.error] = summary.reasons.get(summary.error, 0) + 1
    for observation in recorded:
        try:
            visible = await recorder.latest(observation.pair.pair_id)
        except (ValueError, SQLAlchemyError, OSError):
            # A readback failure does not retroactively invalidate durable writes.
            summary.error = "readback_failed"
            summary.failed += 1
            summary.reasons[summary.error] = summary.reasons.get(summary.error, 0) + 1
            continue
        if visible is not None and visible.id == observation.id:
            summary.readable += 1
        else:
            summary.unavailable += 1
    return summary


async def run(
    settings: Settings,
    *,
    clock: Clock | None = None,
    transport: GeckoTerminalTransport | None = None,
) -> tuple[IngestionSummary, ...]:
    if settings.market_provider != "geckoterminal":
        raise ConfigurationError()
    chains = selected_chains(settings)
    clock = clock if clock is not None else SystemClock()
    results = []
    engine, sessions = connect(settings.database_url)
    try:
        async with transport or GeckoTerminalTransport(settings, clock=clock) as transport:
            directory = NetworkDirectory(transport, settings)
            recorder = MarketRecorder(sessions, clock=clock)
            aborted = False
            for chain in chains:
                if aborted:
                    results.append(
                        IngestionSummary(
                            chain.name, failed=1, error="pass_aborted", reasons={"pass_aborted": 1}
                        )
                    )
                    continue
                adapter = GeckoTerminalAdapter(transport, directory, chain, settings, clock=clock)
                summary = await ingest_chain(adapter, recorder)
                results.append(summary)
                aborted = summary.error not in (None, "unsupported_network")
    finally:
        await engine.dispose()
    return tuple(results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("geckoterminal",))
    parser.add_argument("--chain", choices=("robinhood", "bsc", "all"))
    parser.add_argument("--once", action="store_true", required=True)
    args = parser.parse_args()
    overrides: dict[str, str] = {}
    if args.provider is not None:
        overrides["market_provider"] = args.provider
    if args.chain is not None:
        overrides["market_chains"] = "robinhood,bsc" if args.chain == "all" else args.chain
    try:
        settings = Settings.model_validate({**Settings().model_dump(), **overrides})
        results = asyncio.run(run(settings))
    except (ValidationError, ProviderError, SQLAlchemyError, OSError, ValueError):
        print("error=ingestion_configuration_or_startup_failed")
        return 1
    for result in results:
        print(result.safe_text())
    return 1 if any(result.failed for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
