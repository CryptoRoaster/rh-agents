from typing import Literal

from src.core.models import Contract


class Component(Contract):
    name: str
    role: str
    kind: Literal["agent", "deterministic_service", "infrastructure"]
    status: Literal["PLANNED", "PAPER_READY"]


COMPONENTS = (
    Component(name="ORBIT", role="Market opportunity discovery", kind="agent", status="PLANNED"),
    Component(
        name="ATLAS",
        role="On-chain, holder and wallet intelligence",
        kind="agent",
        status="PLANNED",
    ),
    Component(name="SIGNAL", role="Sentiment and real demand", kind="agent", status="PLANNED"),
    Component(
        name="VECTOR", role="Entry, invalidation and targets", kind="agent", status="PLANNED"
    ),
    Component(name="PULSE", role="Entry and exit triggers", kind="agent", status="PLANNED"),
    Component(
        name="ANCHOR", role="Liquidity, routing and slippage", kind="agent", status="PLANNED"
    ),
    Component(
        name="SENTINEL",
        role="Hard risk controls",
        kind="deterministic_service",
        status="PAPER_READY",
    ),
    Component(name="FUSE", role="Structured trade proposals", kind="agent", status="PLANNED"),
    Component(
        name="COMMANDER", role="Autonomous lifecycle orchestration", kind="agent", status="PLANNED"
    ),
    Component(
        name="LEDGER",
        role="Accounting and reconciliation",
        kind="deterministic_service",
        status="PAPER_READY",
    ),
    Component(
        name="EXECUTOR", role="Paper fill simulation", kind="infrastructure", status="PAPER_READY"
    ),
)
