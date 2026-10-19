"""Advance the PoolManager-wide V4 pool index once, within a request budget.

    python -m src.agents.atlas.v4.index_job --chain robinhood --max-requests N

One run per call; nothing schedules it. It reads only the chain's configured
RPC (Initialize logs of the verified PoolManager, block hashes, code presence)
and writes only the two index tables. The first run locates and verifies the
PoolManager's deployment block and starts there; every later run resumes where
the last confirmed one ended, after re-checking that block's hash. Exhausting
the budget keeps what was confirmed.

Exit codes: ``0`` the index reached the confirmed head, ``3`` partial (budget),
``1`` failed or not configured. Output is one JSON object of codes and numbers.
"""

import argparse
import asyncio
import json

from src.agents.atlas.v4.pool_index import V4PoolIndexer
from src.agents.atlas.v4.pool_index_sql import SqlPoolIndexStore
from src.agents.atlas.v4.protocol import V4_DEPLOYMENTS
from src.agents.atlas.v4.rpc import RpcV4ChainReads
from src.core.config import Settings
from src.data.database import connect
from src.runtime.models import chain_configs
from src.runtime.rpc import EvmRpcClient

COMPLETE, FAILED, PARTIAL = 0, 1, 3
MAX_REQUESTS_CAP = 2_000


async def run(chain: str, max_requests: int) -> tuple[int, dict[str, object]]:
    settings = Settings()
    config = next((item for item in chain_configs(settings) if item.chain == chain), None)
    if config is None:
        return FAILED, {"status": "FAILED", "reason": "CHAIN_NOT_CONFIGURED"}
    deployment = V4_DEPLOYMENTS.get((config.chain, config.chain_id))
    if deployment is None:
        return FAILED, {"status": "FAILED", "reason": "V4_DEPLOYMENT_NOT_CONFIGURED"}
    engine, sessions = connect(settings.database_url)
    client = EvmRpcClient(config, settings)
    try:
        result = await V4PoolIndexer(
            reads=RpcV4ChainReads(client),
            store=SqlPoolIndexStore(sessions),
            deployment=deployment,
            confirmations=config.confirmations,
            max_requests=max_requests,
        ).advance()
    finally:
        await client.close()
        await engine.dispose()
    code = {"COMPLETE": COMPLETE, "PARTIAL": PARTIAL}.get(result.status, FAILED)
    return code, {
        "status": result.status,
        "through_block": result.through_block,
        "requests": result.requests,
        "rewound": result.rewound,
        "reason": result.reason,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--chain", required=True)
    parser.add_argument("--max-requests", type=int, required=True)
    arguments = parser.parse_args()
    if not 0 < arguments.max_requests <= MAX_REQUESTS_CAP:
        print(json.dumps({"status": "FAILED", "reason": "MAX_REQUESTS_OUT_OF_RANGE"}))
        raise SystemExit(FAILED)
    try:
        code, output = asyncio.run(run(arguments.chain, arguments.max_requests))
    except KeyboardInterrupt:
        # systemd stops the unit with SIGINT. Each window commits on its own,
        # so nothing is half-stored; the next run resumes from the last one.
        print(json.dumps({"status": "FAILED", "reason": "INDEX_INTERRUPTED"}))
        raise SystemExit(FAILED) from None
    print(json.dumps(output, sort_keys=True))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
