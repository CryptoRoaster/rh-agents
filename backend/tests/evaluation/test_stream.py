"""Folding the JSONL event stream, including the sequences that must be refused."""

import json

import pytest

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
        ((THREAD, message("x")), "ITEM_BEFORE_TURN"),
        ((THREAD, TURN, TURN), "TURN_RESTARTED"),
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
