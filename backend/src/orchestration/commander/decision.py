"""The deterministic orchestration decision. No model, no strategy, no authority.

Given authoritative state that other components already established, this says
what coordination should do. It is a pure function: the same state always yields
the same decision, which is what lets a decision be checked rather than trusted.

The ordering is the argument.

1. **System stops first.** A kill switch or a recorded pause outranks everything
   a case might otherwise be eligible for.
2. **Then the evaluator's verdict.** Terminal, blocked and rejected cases are
   not situations coordination improves; they are answers.
3. **Then what the workflow is waiting for**, read from the status the evaluator
   published rather than re-derived from evidence.
4. **Only then eligibility**, and today that path ends in an honest stop.

Nothing here reads a finding. Blockers, freshness, requirement satisfaction and
status all arrive already decided; re-deriving any of them would put a second
authority beside the one that owns it.
"""

from datetime import datetime

from src.orchestration.commander.models import (
    CommanderContext,
    CommanderDecision,
    CommanderDisposition,
    CommanderReason,
)
from src.orchestration.commander.policy import CommanderControlPolicy
from src.orchestration.workflow.models import TERMINAL_CASE_STATUSES, TradeCaseStatus

# What each waiting status means for coordination. Read from the evaluator's
# published status rather than recomputed, so there is exactly one place where
# "what is this case waiting for" is decided.
WAITING: dict[TradeCaseStatus, tuple[CommanderDisposition, CommanderReason]] = {
    TradeCaseStatus.DISCOVERED: (
        CommanderDisposition.AWAIT_SPECIALISTS,
        CommanderReason.REQUIRED_EVIDENCE_PENDING,
    ),
    TradeCaseStatus.EVIDENCE_PENDING: (
        CommanderDisposition.AWAIT_SPECIALISTS,
        CommanderReason.REQUIRED_EVIDENCE_PENDING,
    ),
    TradeCaseStatus.READY_FOR_TRIGGER: (
        CommanderDisposition.AWAIT_TRIGGER,
        CommanderReason.WAITING_FOR_TRIGGER,
    ),
    TradeCaseStatus.TRIGGERED: (
        CommanderDisposition.AWAIT_EXECUTION_EVIDENCE,
        CommanderReason.WAITING_FOR_EXECUTION_EVIDENCE,
    ),
    TradeCaseStatus.EXECUTION_EVIDENCE_PENDING: (
        CommanderDisposition.AWAIT_EXECUTION_EVIDENCE,
        CommanderReason.WAITING_FOR_EXECUTION_EVIDENCE,
    ),
}

STATEMENTS: dict[CommanderReason, str] = {
    CommanderReason.REQUIRED_EVIDENCE_PENDING: (
        "Required specialist evidence is not yet complete; the specialists own that."
    ),
    CommanderReason.SAFETY_EVIDENCE_BLOCKED: (
        "Safety evidence records a blocking finding, which coordination cannot clear."
    ),
    CommanderReason.WAITING_FOR_TRIGGER: (
        "The setup is current and its condition has not been met; PULSE owns that watch."
    ),
    CommanderReason.WAITING_FOR_EXECUTION_EVIDENCE: (
        "Execution conditions are still being assessed; ANCHOR owns that assessment."
    ),
    CommanderReason.AUTONOMOUS_SIZING_INPUT_MISSING: (
        "Every workflow prerequisite is met, but no component produces a requested "
        "trade size, so SENTINEL cannot be asked."
    ),
    CommanderReason.RISK_AUTHORIZATION_CURRENT: (
        "A current authorization already covers this exact evidence set."
    ),
    CommanderReason.RISK_REJECTED: (
        "SENTINEL rejected this case; coordination does not reconsider that."
    ),
    CommanderReason.TRADE_CASE_TERMINAL: "The case is finished; nothing progresses it.",
    CommanderReason.SYSTEM_PAUSED: "A system-wide stop is in force.",
    CommanderReason.OBSERVE_MODE: (
        "The deployment is in OBSERVE mode: it watches and does not act."
    ),
}


def decide(
    context: CommanderContext,
    now: datetime,
    policy: CommanderControlPolicy,
) -> CommanderDecision:
    """What coordination should do about this case, and why."""
    disposition, reason = _conclude(context)
    return CommanderDecision(
        policy_version=policy.version,
        trade_case_id=context.trade_case_id,
        disposition=disposition,
        reason_code=reason,
        statement=STATEMENTS[reason],
        context_digest=context.context_digest,
        decided_at=now,
    )


def _conclude(
    context: CommanderContext,
) -> tuple[CommanderDisposition, CommanderReason]:
    # A system-wide stop outranks every case-level eligibility. Checked first so
    # no later branch can reach past it.
    if context.controls.halted:
        reason = (
            CommanderReason.OBSERVE_MODE
            if context.controls.trading_mode == "OBSERVE"
            and not context.controls.kill_switch
            and not context.controls.account_paused
            else CommanderReason.SYSTEM_PAUSED
        )
        return CommanderDisposition.PAUSED, reason

    if context.status in TERMINAL_CASE_STATUSES:
        reason = (
            CommanderReason.RISK_REJECTED
            if context.status == TradeCaseStatus.RISK_REJECTED
            else CommanderReason.TRADE_CASE_TERMINAL
        )
        return CommanderDisposition.HALTED, reason

    if context.status == TradeCaseStatus.BLOCKED:
        # The evaluator found a blocking fact. Coordination has no opinion that
        # could outrank one, and the advisory synthesis has none either.
        return CommanderDisposition.HALTED, CommanderReason.SAFETY_EVIDENCE_BLOCKED

    if (waiting := WAITING.get(context.status)) is not None:
        return waiting

    if context.status in (
        TradeCaseStatus.RISK_APPROVED,
        TradeCaseStatus.RISK_LIMITED,
    ):
        # An authorization exists. It applies only if it covers this evidence
        # *and* has not expired — identity alone would let a decision SENTINEL
        # issued with a two-minute life read as current forever.
        if context.risk is not None and context.risk.is_usable:
            return CommanderDisposition.RISK_CURRENT, CommanderReason.RISK_AUTHORIZATION_CURRENT
        return _risk_eligible()

    if context.status == TradeCaseStatus.READY_FOR_RISK:
        # The evaluator publishes READY_FOR_RISK precisely when no usable
        # authorization covers the case — including when a previous one aged
        # out. Reading an old binding as current here would reinstate the
        # authorization the evaluator just withdrew.
        if context.risk is not None and context.risk.is_usable:
            return CommanderDisposition.RISK_CURRENT, CommanderReason.RISK_AUTHORIZATION_CURRENT
        return _risk_eligible()

    # An unmapped status is not a licence to improvise.
    return CommanderDisposition.AWAIT_SPECIALISTS, CommanderReason.REQUIRED_EVIDENCE_PENDING


def _risk_eligible() -> tuple[CommanderDisposition, CommanderReason]:
    """The honest stop, and the one place a sizing policy would ever be read.

    Every workflow prerequisite is satisfied and asking SENTINEL is the next
    step. Asking requires a requested trade size, and nothing in this system
    produces one: VECTOR's schema forbids proposing a size, ANCHOR reports what
    the market bears rather than what to trade, and SENTINEL's own cap is a
    ceiling rather than an instruction. Any number invented here would be a
    sizing strategy wearing a coordinator's name, so the gap is reported.
    """
    return (
        CommanderDisposition.BLOCKED_ON_MISSING_CAPABILITY,
        CommanderReason.AUTONOMOUS_SIZING_INPUT_MISSING,
    )
