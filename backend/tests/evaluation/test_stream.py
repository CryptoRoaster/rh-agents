"""Folding the JSONL event stream, including the sequences that must be refused."""

import json
from pathlib import Path

import pytest

from src.evaluation.codex import diagnostics
from src.evaluation.codex.diagnostics import (
    MASKED_VALUE,
    REDACTED_LINE,
    REDACTION_FAILED,
    PathAliases,
)
from src.evaluation.codex.models import EvaluationFailure
from src.evaluation.codex.stream import EventAccumulator, StreamError


def accumulator(limit: int = 65_536) -> EventAccumulator:
    return EventAccumulator(max_final_message_bytes=limit)


def feed(state: EventAccumulator, *events: dict[str, object]) -> None:
    for event in events:
        state.feed(json.dumps(event).encode("utf-8"))


THREAD = {"type": "thread.started", "thread_id": "t-1"}
TURN = {"type": "turn.started"}


def message(text: str, item_id: str = "i1") -> dict[str, object]:
    return {
        "type": "item.completed",
        "item": {"id": item_id, "type": "agent_message", "text": text},
    }


def todo(stage: str, item_id: str = "todo-1") -> dict[str, object]:
    """The item Codex emits from `TurnPlanUpdated`, which precedes the turn.

    `EventProcessorWithJsonOutput` maps that notification straight onto an
    `ItemStarted` carrying a `TodoListItem`, without waiting for a
    `TurnStarted`. This is the shape that the fourth real probe met and that
    the parser used to refuse.
    """
    return {
        "type": f"item.{stage}",
        "item": {"id": item_id, "type": "todo_list", "items": []},
    }


def test_a_todo_item_before_the_turn_is_ordinary() -> None:
    """Case 1: the sequence a real 0.153.4 run actually produces."""
    state = accumulator()
    feed(state, THREAD, todo("started"), TURN, message("final"), {"type": "turn.completed"})
    assert state.require_consistent_completion() == "final"
    assert state.thread_id == "t-1"


def test_a_whole_pre_turn_todo_lifecycle_is_ordinary() -> None:
    """Case 2: started and updated before the turn, completed after it."""
    state = accumulator()
    feed(
        state,
        THREAD,
        todo("started"),
        todo("updated"),
        TURN,
        todo("completed"),
        message("final"),
        {"type": "turn.completed"},
    )
    assert state.require_consistent_completion() == "final"


def test_a_pre_turn_item_is_not_an_implicit_turn_start() -> None:
    """Case 3: the loosened rule must not become a loosened success rule.

    Accepting the item and inferring a turn from it would be two changes, and
    only the first one is warranted. An item says the CLI emitted events; it
    says nothing about a turn having begun.
    """
    state = accumulator()
    feed(state, THREAD, todo("started"))
    assert state.turn_started is False
    with pytest.raises(StreamError) as caught:
        state.require_consistent_completion()
    assert caught.value.failure is EvaluationFailure.INCONSISTENT_COMPLETION
    assert caught.value.reason_code == "TURN_NEVER_STARTED"


def test_an_agent_message_before_the_turn_is_kept_but_never_suffices() -> None:
    """Case 7: structurally folded in, and still not a result on its own.

    The message is retained -- refusing to read it would be the old rule under
    another name -- but the run only succeeds once the explicit `turn.started`
    and `turn.completed` have both arrived.
    """
    state = accumulator()
    feed(state, THREAD, message("early"))
    assert state.final_message == "early"
    with pytest.raises(StreamError) as caught:
        state.require_consistent_completion()
    assert caught.value.reason_code == "TURN_NEVER_STARTED"

    feed(state, TURN, {"type": "turn.completed"})
    assert state.require_consistent_completion() == "early"


def test_a_tool_item_before_the_turn_is_still_observed() -> None:
    """Observation of the stream, not entitlement to the tool.

    A tool-shaped item that arrives early is recorded exactly as a later one
    would be. Dropping it because of where it sat in the stream would make the
    observation quieter than the run actually was.
    """
    state = accumulator()
    feed(
        state,
        THREAD,
        {"type": "item.started", "item": {"id": "c1", "type": "command_execution"}},
        TURN,
        {"type": "item.completed", "item": {"id": "c1", "type": "command_execution"}},
        message("final"),
        {"type": "turn.completed"},
    )
    observed = state.observed_tool_activity()
    assert [(item.item_type, item.count) for item in observed] == [("command_execution", 1)]


def test_usage_is_read_only_where_reported() -> None:
    state = accumulator()
    feed(
        state,
        THREAD,
        TURN,
        message("{}"),
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 12, "cached_input_tokens": 0, "output_tokens": 5},
        },
    )
    assert state.usage.input_tokens == 12
    assert state.usage.output_tokens == 5


def test_missing_usage_stays_none() -> None:
    state = accumulator()
    feed(state, THREAD, TURN, message("{}"), {"type": "turn.completed"})
    assert state.usage.input_tokens is None
    assert state.usage.cached_input_tokens is None
    assert state.usage.output_tokens is None


def test_malformed_usage_values_stay_none() -> None:
    state = accumulator()
    feed(
        state,
        THREAD,
        TURN,
        message("{}"),
        {"type": "turn.completed", "usage": {"input_tokens": "many", "output_tokens": -4}},
    )
    assert state.usage.input_tokens is None
    assert state.usage.output_tokens is None


def test_model_and_effort_stay_none_when_not_reported() -> None:
    state = accumulator()
    feed(state, THREAD, TURN, message("{}"), {"type": "turn.completed"})
    assert state.reported_model is None
    assert state.reported_effort is None


def test_the_last_message_before_the_turn_ends_is_authoritative() -> None:
    state = accumulator()
    feed(
        state,
        THREAD,
        TURN,
        message("draft", "i0"),
        message("final", "i1"),
        {"type": "turn.completed"},
    )
    assert state.require_consistent_completion() == "final"


def test_a_message_without_a_completed_turn_is_not_a_result() -> None:
    state = accumulator()
    feed(state, THREAD, TURN, message("final"))
    with pytest.raises(StreamError) as caught:
        state.require_consistent_completion()
    assert caught.value.failure is EvaluationFailure.INCONSISTENT_COMPLETION
    assert caught.value.reason_code == "TURN_NEVER_COMPLETED"


def test_a_completed_turn_without_a_message_is_not_a_result() -> None:
    state = accumulator()
    feed(state, THREAD, TURN, {"type": "turn.completed"})
    with pytest.raises(StreamError) as caught:
        state.require_consistent_completion()
    assert caught.value.failure is EvaluationFailure.NO_FINAL_MESSAGE


def test_turn_failure_is_surfaced_immediately() -> None:
    state = accumulator()
    with pytest.raises(StreamError) as caught:
        feed(state, THREAD, TURN, {"type": "turn.failed", "error": {"message": "refused"}})
    assert caught.value.failure is EvaluationFailure.TURN_FAILED


@pytest.mark.parametrize(
    ("events", "reason"),
    [
        (({"type": "turn.completed"},), "TURN_END_WITHOUT_START"),
        # A pre-turn item is no longer on this list; it is ordinary. What is
        # still refused is a `turn.completed` that no `turn.started` preceded,
        # including when items arrived in between.
        (
            (THREAD, todo("started"), {"type": "turn.completed"}),
            "TURN_END_WITHOUT_START",
        ),
        ((THREAD, TURN, TURN), "TURN_RESTARTED"),
        (
            (THREAD, TURN, {"type": "turn.completed"}, todo("started")),
            "ITEM_AFTER_TURN",
        ),
        (
            (THREAD, TURN, {"type": "turn.completed"}, {"type": "turn.completed"}),
            "TURN_ENDED_TWICE",
        ),
        ((THREAD, THREAD), "THREAD_RESTARTED"),
        ((THREAD, TURN, {"type": "item.completed", "item": {"id": "x"}}), "ITEM_TYPE_MISSING"),
    ],
)
def test_contradictory_sequences_are_refused(
    events: tuple[dict[str, object], ...], reason: str
) -> None:
    state = accumulator()
    with pytest.raises(StreamError) as caught:
        feed(state, *events)
    assert caught.value.reason_code == reason


def test_a_corrupt_line_is_refused() -> None:
    state = accumulator()
    with pytest.raises(StreamError) as caught:
        state.feed(b"this is not json\n")
    assert caught.value.failure is EvaluationFailure.EVENT_STREAM_INVALID


def test_unknown_event_types_are_ignored() -> None:
    state = accumulator()
    feed(
        state,
        THREAD,
        TURN,
        {"type": "something.new", "payload": 1},
        message("{}"),
        {"type": "turn.completed"},
    )
    assert state.require_consistent_completion() == "{}"


def test_the_parser_budget_is_separate_from_the_stream_cap() -> None:
    state = accumulator(limit=64)
    with pytest.raises(StreamError) as caught:
        feed(state, THREAD, TURN, message("q" * 200))
    assert caught.value.failure is EvaluationFailure.PARSER_BUDGET_EXCEEDED


def test_tool_items_are_counted_once_per_item_id() -> None:
    state = accumulator()
    started = {
        "type": "item.started",
        "item": {"id": "c1", "type": "command_execution", "command": "ls"},
    }
    completed = {
        "type": "item.completed",
        "item": {"id": "c1", "type": "command_execution", "command": "ls"},
    }
    search = {"type": "item.completed", "item": {"id": "w1", "type": "web_search", "query": "x"}}
    feed(state, THREAD, TURN, started, completed, search)
    activity = {item.item_type: item.count for item in state.observed_tool_activity()}
    assert activity == {"command_execution": 1, "web_search": 1}


def test_no_tool_activity_is_not_evidence_of_no_tools() -> None:
    state = accumulator()
    feed(state, THREAD, TURN, message("{}"), {"type": "turn.completed"})
    # The stream reports tool *use*. An offered-but-unused tool emits nothing,
    # so this empty tuple says nothing about what the model was offered.
    assert state.observed_tool_activity() == ()


# --------------------------------------------------------------------------
# A Codex `error` event: what it is allowed to say, and what it must not
# --------------------------------------------------------------------------


def stream_error(state: EventAccumulator, message: object) -> StreamError:
    """Feed one `error` event and return the exception it raised."""
    with pytest.raises(StreamError) as caught:
        feed(state, THREAD, {"type": "error", "message": message})
    return caught.value


def test_a_stream_error_reports_what_codex_said(tmp_path: Path) -> None:
    """Case 1: the message survives, redacted, instead of being dropped.

    The fifth real probe ended on exactly this event and could report nothing
    but the reason code: the message was read, used to choose between two
    codes, and then discarded. The code alone says a failure happened and
    nothing about which one.
    """
    state = accumulator()
    error = stream_error(state, "provider rejected request")
    assert error.failure is EvaluationFailure.PROCESS_FAILED
    assert error.reason_code == "STREAM_ERROR"
    assert error.safe_lines == ("provider rejected request",)


@pytest.mark.parametrize(
    "message",
    [
        "Authorization: Bearer abc123",
        "request failed: access_token=xyz",
        "set-cookie: session=nope",
        "api_key rejected",
        '{"error": {"message": "bad password"}}',
    ],
)
def test_a_credential_shaped_message_is_dropped_whole(message: str) -> None:
    """Case 2: the line is replaced, never edited, and the secret never leaks.

    Editing assumes the shape of the value, and a shape assumption is what
    fails on the message nobody anticipated. Checked against every surface a
    caller could reach: the lines, the exception's text and its repr.
    """
    state = accumulator()
    error = stream_error(state, message)
    assert error.reason_code == "STREAM_ERROR"
    assert error.safe_lines == (REDACTED_LINE,)
    for surface in (str(error), repr(error), " ".join(error.safe_lines)):
        assert "abc123" not in surface
        assert "xyz" not in surface
        assert "nope" not in surface


def test_a_token_shaped_value_is_masked() -> None:
    """Case 3: what survives the line check is still masked defensively.

    A JWT and any long unbroken opaque run are replaced. Blunt on purpose -- a
    masked build identifier costs nothing.
    """
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27u"
    error = stream_error(accumulator(), f"upstream said {jwt} and gave up")
    assert jwt not in " ".join(error.safe_lines)
    assert MASKED_VALUE in error.safe_lines[0]

    opaque = "z" * 48
    other = stream_error(accumulator(), f"handle {opaque} expired")
    assert opaque not in " ".join(other.safe_lines)
    assert MASKED_VALUE in other.safe_lines[0]


def test_an_absolute_harness_path_becomes_an_alias(tmp_path: Path) -> None:
    """Case 4: a diagnostic names roles, not where this machine keeps things."""
    home = tmp_path / "codex-home"
    home.mkdir()
    state = EventAccumulator(
        max_final_message_bytes=65_536,
        aliases=PathAliases.build(codex_home=home),
    )
    error = stream_error(state, f"could not read {home}/auth.json")
    joined = " ".join(error.safe_lines)
    assert str(home) not in joined
    assert "<CODEX_HOME>" in joined


def test_a_broken_redactor_yields_the_marker_and_never_the_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case 5: fail closed, including when the redactor itself raises.

    There is no path on which the unredacted message reaches a caller. This
    breaks redaction from the inside rather than trusting that it cannot fail.
    """

    class Exploding:
        def sub(self, *_args: object, **_kwargs: object) -> str:
            raise RuntimeError("redactor is broken")

    monkeypatch.setattr(diagnostics, "ANSI", Exploding())
    error = stream_error(accumulator(), "a very specific secret detail")
    assert error.safe_lines == (REDACTION_FAILED,)
    assert "specific secret detail" not in repr(error)


def test_a_malformed_message_keeps_its_own_code() -> None:
    """A non-string message is still its own answer, with nothing to redact."""
    error = stream_error(accumulator(), {"not": "a string"})
    assert error.reason_code == "STREAM_ERROR_MALFORMED"
    assert error.safe_lines == ()


def test_the_state_before_the_error_is_still_readable() -> None:
    """Cases 6 and 9: no turn yet, a thread id, and no tool activity."""
    state = accumulator()
    stream_error(state, "boom")
    assert state.turn_started is False
    assert state.thread_id == "t-1"
    assert state.observed_tool_activity() == ()


def test_a_turn_that_had_started_is_visible_after_the_error() -> None:
    """Case 7: the one fact that made the fifth probe's report UNKNOWN."""
    state = accumulator()
    feed(state, THREAD, TURN)
    with pytest.raises(StreamError):
        feed(state, {"type": "error", "message": "boom"})
    assert state.turn_started is True
    assert state.thread_id == "t-1"


def test_tool_activity_seen_before_the_error_is_still_counted() -> None:
    """Case 8: observation up to the abort, which is not the same as none."""
    state = accumulator()
    feed(
        state,
        THREAD,
        {"type": "item.started", "item": {"id": "c1", "type": "command_execution"}},
        TURN,
    )
    with pytest.raises(StreamError):
        feed(state, {"type": "error", "message": "boom"})
    observed = state.observed_tool_activity()
    assert [(item.item_type, item.count) for item in observed] == [("command_execution", 1)]
