"""COMMANDER: the deterministic control plane for the TradeCase lifecycle.

It answers one operational question — *what safe workflow action, if any, is
currently eligible?* — from state that other components already established, and
it opens cases from recorded market candidates so the system has a way to begin.

It is not a second workflow engine. The Phase 2A evaluator owns every status,
requirement, blocker and freshness rule; COMMANDER reads that verdict rather
than recomputing it, because two authorities on one question only have to
disagree once.

It is not strategy. No technical rule, no sentiment reading, no holder score, no
entry selection, no route preference, no probability. Intake decides whether the
machinery may open a case — supported chain, valid canonical identity, fresh
observation, no active duplicate, bounded per cycle — and never which candidate
looks better. ORBIT exists to judge that, and a coordinator that pre-filtered on
market grounds would be a second analyst nobody could audit.

It is not an authority. It cannot write a status, clear a blocker, overrule
SENTINEL, reach a wallet, build a transaction or send one. There is no model
here and no provider.

It does not size trades, and cannot. Asking SENTINEL requires a requested trade
size, and nothing in this system produces one: VECTOR's schema forbids proposing
a size, ANCHOR reports what the market bears rather than what to trade, and
SENTINEL's own cap is a ceiling rather than an instruction. Rather than invent a
number, the control plane stops at that point and reports the gap.
"""

from src.orchestration.commander.context import (
    CommanderContextReader,
    CommanderContextUnavailable,
    context_digest,
)
from src.orchestration.commander.decision import decide
from src.orchestration.commander.intake import (
    CommanderIntakeService,
    IntakeOutcome,
    IntakeRefusal,
)
from src.orchestration.commander.models import (
    CommanderContext,
    CommanderDecision,
    CommanderDisposition,
    CommanderReason,
)
from src.orchestration.commander.policy import COMMANDER_CONTROL_V1, CommanderControlPolicy

__all__ = [
    "COMMANDER_CONTROL_V1",
    "CommanderContext",
    "CommanderContextReader",
    "CommanderContextUnavailable",
    "CommanderControlPolicy",
    "CommanderDecision",
    "CommanderDisposition",
    "CommanderIntakeService",
    "CommanderReason",
    "IntakeOutcome",
    "IntakeRefusal",
    "context_digest",
    "decide",
]
