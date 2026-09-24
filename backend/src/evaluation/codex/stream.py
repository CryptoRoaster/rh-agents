"""Read the Codex 0.153.4 JSONL event stream and decide whether a turn finished.

The event contract this parser is written against:

    thread.started   {"type", "thread_id"}
    turn.started     {"type"}
    item.started     {"type", "item": {"id", "type", ...}}
    item.updated     {"type", "item": {...}}
    item.completed   {"type", "item": {...}}
    turn.completed   {"type", "usage": {"input_tokens",
                                        "cached_input_tokens",
                                        "output_tokens"}}
    turn.failed      {"type", "error": {"message"}}
    error            {"type", "message"}

Item types that indicate tool use are `command_execution`, `file_change`,
`mcp_tool_call` and `web_search`. They are counted by distinct item id, so an
item that starts and completes is one observation rather than two, and an item
that starts and never completes is still observed.

The order of these is looser than it looks. `item.started`, `item.updated` and
`item.completed` can arrive **before** `turn.started`: the JSONL processor maps
`ServerNotification::TurnPlanUpdated` directly onto an `ItemStarted` carrying a
`TodoListItem`, with no requirement that a `TurnStarted` notification came
first. This parser used to refuse that as `ITEM_BEFORE_TURN`, and the fourth
authorised real probe died on it after the CLI had otherwise got all the way to
emitting events. Pre-turn items are now folded in like any other.

Success needs a consistent ending, not merely a message: the turn must have
started, an `agent_message` item must have completed, and `turn.completed` must
have arrived. Relaxing the ordering did not relax any of that. A pre-turn item
is never read as an implicit `turn.started`, so items alone can never produce a
result -- `TURN_NEVER_STARTED` still stands, and so does
`TURN_END_WITHOUT_START` for a `turn.completed` that no `turn.started`
preceded. An earlier `agent_message` on its own proves nothing -- the model may
speak again, and the turn may still fail afterwards -- so the last completed
`agent_message` before `turn.completed` is the authoritative answer.

Contradictions are rejected. Unknown top-level event types are ignored instead,
because a stricter rule would turn any additive change in a future build into a
crash; the version pin in `models.SUPPORTED_CLI_VERSION` is what guards against
a build whose contract actually changed.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from src.evaluation.codex.diagnostics import EMPTY_ALIASES, PathAliases, redact
from src.evaluation.codex.models import (
    EvaluationFailure,
    EvaluationUsage,
    ObservedToolActivity,
)

TOOL_ITEM_TYPES = frozenset({"command_execution", "file_change", "mcp_tool_call", "web_search"})


class StreamError(Exception):
    """The event stream cannot be interpreted, or the turn did not succeed.

    `safe_lines` carries **already redacted** text and nothing else. There is no
    raw counterpart, and the exception's own message is the failure and the
    reason code, so neither `str(error)` nor `repr(error)` can ever show what
    the CLI actually wrote.
    """

    def __init__(
        self,
        failure: EvaluationFailure,
        reason_code: str,
        safe_lines: tuple[str, ...] = (),
    ) -> None:
        self.failure = failure
        self.reason_code = reason_code
        self.safe_lines = safe_lines
        super().__init__(f"{failure.value}:{reason_code}")


@dataclass
class EventAccumulator:
    """Fold the event stream into the few facts the harness reports.

    `max_final_message_bytes` is a parser budget and is deliberately separate
    from the raw stdout cap: the stream cap bounds what is read off the pipe,
    this one bounds what is retained in memory afterwards.
    """

    max_final_message_bytes: int
    # The running client's bound paths. A Codex `error` event is reported
    # through the same redaction as stderr, and without these the absolute
    # paths in it would be reported verbatim.
    aliases: PathAliases = EMPTY_ALIASES
    thread_id: str | None = None
    reported_model: str | None = None
    reported_effort: str | None = None
    turn_started: bool = False
    turn_completed: bool = False
    final_message: str | None = None
    usage: EvaluationUsage = field(default_factory=EvaluationUsage)
    tool_items: dict[str, set[str]] = field(default_factory=dict)

    def feed(self, line: bytes) -> None:
        text = line.strip()
        if not text:
            return
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            raise StreamError(EvaluationFailure.EVENT_STREAM_INVALID, "LINE_NOT_JSON") from None
        if not isinstance(event, dict):
            raise StreamError(EvaluationFailure.EVENT_STREAM_INVALID, "EVENT_NOT_OBJECT")

        kind = event.get("type")
        if not isinstance(kind, str):
            raise StreamError(EvaluationFailure.EVENT_STREAM_INVALID, "EVENT_TYPE_MISSING")

        if kind == "thread.started":
            self._thread_started(event)
        elif kind == "turn.started":
            self._turn_started()
        elif kind in {"item.started", "item.updated", "item.completed"}:
            self._item(kind, event)
        elif kind == "turn.completed":
            self._turn_completed(event)
        elif kind == "turn.failed":
            self._turn_failed(event)
        elif kind == "error":
            self._stream_error(event)
        # Any other type is additive and ignored on purpose.

    def _stream_error(self, event: dict[str, Any]) -> None:
        """Codex reported a failure of its own. Say what it was, safely.

        The fifth real probe ended here and could report nothing but the code:
        the message was recognised, used to pick between two reason codes, and
        then dropped. A failure that cannot be described is a failure that gets
        retried blindly.

        So the message goes through exactly the redaction stderr goes through
        -- the same sensitive-term rules, the same JWT and long-opaque masking,
        the same path aliases, the same line and size budgets, and the same
        fail-closed guarantee. The raw string is never stored, never logged and
        never reaches the exception's own message.

        `errors="replace"` on the encode is not decoration: a JSON string may
        contain a lone surrogate, which plain UTF-8 encoding refuses, and a
        diagnostic must not be able to raise a second failure.
        """
        message = event.get("message")
        if not isinstance(message, str):
            raise StreamError(EvaluationFailure.PROCESS_FAILED, "STREAM_ERROR_MALFORMED")
        raise StreamError(
            EvaluationFailure.PROCESS_FAILED,
            "STREAM_ERROR",
            redact(message.encode("utf-8", errors="replace"), self.aliases),
        )

    def _thread_started(self, event: dict[str, Any]) -> None:
        if self.thread_id is not None:
            raise StreamError(EvaluationFailure.EVENT_STREAM_INVALID, "THREAD_RESTARTED")
        thread_id = event.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise StreamError(EvaluationFailure.EVENT_STREAM_INVALID, "THREAD_ID_MISSING")
        self.thread_id = thread_id
        # Neither field is part of the documented contract. They are recorded
        # only if a build actually sends them, and stay None otherwise.
        model = event.get("model")
        if isinstance(model, str) and model:
            self.reported_model = model
        effort = event.get("effort")
        if isinstance(effort, str) and effort:
            self.reported_effort = effort

    def _turn_started(self) -> None:
        if self.turn_started:
            raise StreamError(EvaluationFailure.EVENT_STREAM_INVALID, "TURN_RESTARTED")
        if self.turn_completed:
            raise StreamError(EvaluationFailure.EVENT_STREAM_INVALID, "TURN_STARTED_AFTER_END")
        self.turn_started = True

    def _item(self, kind: str, event: dict[str, Any]) -> None:
        # An item before `turn.started` is ordinary, not a contradiction.
        # `EventProcessorWithJsonOutput` turns `ServerNotification::
        # TurnPlanUpdated` straight into `ThreadEvent::ItemStarted` for a
        # `TodoListItem` without requiring that `TurnStarted` was seen first,
        # and Codex's own tests call `collect_thread_events` with exactly that
        # notification alone. Refusing it cost the fourth real probe.
        #
        # What is deliberately *not* done here is infer `turn_started = True`.
        # An item is evidence that the CLI produced events, never evidence that
        # a turn began; `require_consistent_completion` still demands the
        # explicit event, so no sequence of items can add up to a success.
        if self.turn_completed:
            raise StreamError(EvaluationFailure.EVENT_STREAM_INVALID, "ITEM_AFTER_TURN")
        item = event.get("item")
        if not isinstance(item, dict):
            raise StreamError(EvaluationFailure.EVENT_STREAM_INVALID, "ITEM_MISSING")
        item_type = item.get("type")
        if not isinstance(item_type, str) or not item_type:
            raise StreamError(EvaluationFailure.EVENT_STREAM_INVALID, "ITEM_TYPE_MISSING")
        item_id = item.get("id")
        if item_type in TOOL_ITEM_TYPES:
            self.tool_items.setdefault(item_type, set()).add(
                item_id if isinstance(item_id, str) else ""
            )
        if kind == "item.completed" and item_type == "agent_message":
            self._agent_message(item)

    def _agent_message(self, item: dict[str, Any]) -> None:
        text = item.get("text")
        if not isinstance(text, str):
            raise StreamError(EvaluationFailure.EVENT_STREAM_INVALID, "AGENT_MESSAGE_TEXT_MISSING")
        if len(text.encode("utf-8")) > self.max_final_message_bytes:
            raise StreamError(EvaluationFailure.PARSER_BUDGET_EXCEEDED, "FINAL_MESSAGE_TOO_LARGE")
        # A later message replaces an earlier one; only the last one before
        # turn.completed is authoritative.
        self.final_message = text

    def _turn_completed(self, event: dict[str, Any]) -> None:
        if not self.turn_started:
            raise StreamError(EvaluationFailure.EVENT_STREAM_INVALID, "TURN_END_WITHOUT_START")
        if self.turn_completed:
            raise StreamError(EvaluationFailure.EVENT_STREAM_INVALID, "TURN_ENDED_TWICE")
        self.turn_completed = True
        self.usage = _usage(event.get("usage"))

    def _turn_failed(self, event: dict[str, Any]) -> None:
        if self.turn_completed:
            raise StreamError(EvaluationFailure.EVENT_STREAM_INVALID, "TURN_FAILED_AFTER_END")
        error = event.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        raise StreamError(
            EvaluationFailure.TURN_FAILED,
            "TURN_FAILED" if isinstance(message, str) else "TURN_FAILED_MALFORMED",
        )

    def observed_tool_activity(self) -> tuple[ObservedToolActivity, ...]:
        return tuple(
            ObservedToolActivity(item_type=item_type, count=len(ids))
            for item_type, ids in sorted(self.tool_items.items())
        )

    def require_consistent_completion(self) -> str:
        """Return the authoritative answer, or explain why there is none."""
        if not self.turn_started:
            raise StreamError(EvaluationFailure.INCONSISTENT_COMPLETION, "TURN_NEVER_STARTED")
        if not self.turn_completed:
            raise StreamError(EvaluationFailure.INCONSISTENT_COMPLETION, "TURN_NEVER_COMPLETED")
        if self.final_message is None:
            raise StreamError(EvaluationFailure.NO_FINAL_MESSAGE, "NO_AGENT_MESSAGE")
        return self.final_message


def _usage(payload: object) -> EvaluationUsage:
    """Read only what is present. A missing or malformed field stays None."""
    if not isinstance(payload, dict):
        return EvaluationUsage()
    return EvaluationUsage(
        input_tokens=_token_count(payload.get("input_tokens")),
        cached_input_tokens=_token_count(payload.get("cached_input_tokens")),
        output_tokens=_token_count(payload.get("output_tokens")),
    )


def _token_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None
