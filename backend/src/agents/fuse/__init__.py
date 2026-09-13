"""FUSE: the deterministic synthesis of a case's currently admissible evidence.

FUSE reads what the other specialists recorded and states, in one place, what it
adds up to. It is the only role that looks at other roles' findings, and that is
precisely why its limits are structural rather than conventional.

It does not vote. Four specialists answering four different questions have no
common scale, so there is no count, no average, no weight and no score here.
Positive sentiment cannot outweigh a holder-concentration failure, because the
two are not measurements of the same thing.

It cannot clear anything. Blockers and gaps are derived from the sources' own
committed verdicts, and no path in this package removes one. Unknown
safety-critical evidence stays unknown, and unknown fails closed.

It has no authority. No approval, no rejection, no position size, no notional,
no slippage, no route, no risk outcome, no ability to reach a wallet, signer,
executor, SENTINEL or the ledger, and no way to force a case's status. SENTINEL
continues to read the canonical safety evidence directly; a synthesis of that
evidence changes nothing about what SENTINEL sees.

There is no model. Every input is already a structured verdict from a specialist
that did the interpreting, and a second interpretation layer would place a
probabilistic opinion on top of settled answers while leaving nobody able to say
which of the two the system acted on.
"""

from src.agents.fuse.context import FuseContextReader
from src.agents.fuse.handler import FUSE_TASK_TYPE, FuseWorkerHandler
from src.agents.fuse.models import (
    EvidenceSynthesis,
    FuseDisposition,
    FuseReasonCode,
    FuseTaskInput,
)
from src.agents.fuse.policy import FUSE_SYNTHESIS_V1, FuseSynthesisPolicy
from src.agents.fuse.ports import FuseContextPort, FuseContextUnavailable
from src.agents.fuse.synthesis import synthesize

__all__ = [
    "FUSE_SYNTHESIS_V1",
    "FUSE_TASK_TYPE",
    "EvidenceSynthesis",
    "FuseContextPort",
    "FuseContextReader",
    "FuseContextUnavailable",
    "FuseDisposition",
    "FuseReasonCode",
    "FuseSynthesisPolicy",
    "FuseTaskInput",
    "FuseWorkerHandler",
    "synthesize",
]
