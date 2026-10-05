"""Runtime code for custody contracts, built from the pinned official templates.

The fixture files are the unlinked `deployedBytecode` of the launcher's
FeeSplitter and TimelockedPositionRecipient, compiled from commit 7ea523c with
the repository's own settings (see docs/atlas-v4-position-control.md). Filling
their immutable ranges reproduces what the chain holds for a deployment, so
the adapters are tested against the real code, not against a stand-in.
"""

from pathlib import Path

from src.agents.atlas.v4.custody.template import CodeTemplate
from src.agents.atlas.v4.custody.uniswap_launcher import (
    FEE_SPLITTER_TEMPLATE,
    TIMELOCKED_RECIPIENT_TEMPLATE,
)
from tests.atlas.v4.chain import POOL_MANAGER, POSITION_MANAGER

FIXTURES = Path(__file__).parent / "fixtures"
# The README's two v3.3.0 FeeSplitters and one of its v3.2.0 FeeSplitters.
OFFICIAL_SPLITTER = "0x9411fa7f956f64aa7981aa27cb3bc6ec0415449c"
SECOND_OFFICIAL_SPLITTER = "0x882ae5e2095435a62fd1bbdefcb637f5ceafc0ee"
OLDER_OFFICIAL_SPLITTER = "0xeff166aaf189323c58dc27ed1206eb2c37faacdf"
# The same official code, deployed by anyone else at an address of their own.
UNLISTED_SPLITTER = "0x" + "5f" * 20
TIMELOCK_RECIPIENT = "0x" + "71" * 20
OPERATOR = "0x" + "0e" * 20


def template_code(name: str) -> bytes:
    return bytes.fromhex((FIXTURES / f"{name}.v3.3.0.runtime.hex").read_text().strip()[2:])


def fill(template: CodeTemplate, code: bytes, values: dict[str, int]) -> str:
    filled = bytearray(code)
    for name, offsets in template.immutables.items():
        for offset in offsets:
            filled[offset : offset + 32] = values[name].to_bytes(32, "big")
    return "0x" + filled.hex()


def fee_splitter_code(
    *, position_manager: str = POSITION_MANAGER, pool_manager: str = POOL_MANAGER
) -> str:
    return fill(
        FEE_SPLITTER_TEMPLATE,
        template_code("FeeSplitter"),
        {"positionManager": int(position_manager, 16), "poolManager": int(pool_manager, 16)},
    )


def timelocked_code(
    *,
    timelock: int,
    operator: str = OPERATOR,
    position_manager: str = POSITION_MANAGER,
    use_arb_sys: int = 1,
) -> str:
    return fill(
        TIMELOCKED_RECIPIENT_TEMPLATE,
        template_code("TimelockedPositionRecipient"),
        {
            "positionManager": int(position_manager, 16),
            "operator": int(operator, 16),
            "timelockBlockNumber": timelock,
            "_USE_ARB_SYS": use_arb_sys,
        },
    )


def minimal_proxy_to(target: str) -> str:
    """An EIP-1167 clone that delegates every call to ``target``."""
    return "0x363d3d373d3d3d363d73" + target[2:] + "5af43d82803e903d91602b57fd5bf3"
