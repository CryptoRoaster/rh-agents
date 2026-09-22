"""Deadline, cancellation, output caps and what cleanup actually achieves.

The process-group assertions here are the strongest statement this suite can
make, and it is still a narrow one: the group the harness created is gone. A
descendant that deliberately left that group would be invisible to both the
harness and this test, which is why `CleanupReport` documents itself as an
account of what was attempted rather than an operating-system guarantee.
"""

import asyncio
import json
import os
import signal
from collections.abc import Callable
from pathlib import Path

import pytest

from src.evaluation.codex.deadline import Deadline
from src.evaluation.codex.models import EvaluationFailure, OutputLimits
from src.evaluation.codex.process import AbortedByConsumer, ProcessError, run_bounded
from tests.evaluation.conftest import CHILD_PATH_ENTRIES, write_launcher


def environment(root: Path) -> dict[str, str]:
    return {
        "CODEX_HOME": str(root / "codex"),
        "HOME": str(root / "home"),
        "PATH": ":".join(str(entry) for entry in CHILD_PATH_ENTRIES),
        "TMPDIR": str(root / "tmp"),
        "LANG": "en_US.UTF-8",
    }


def workspace(root: Path, scenario: str, **extra: object) -> Path:
    directory = root / "workspace"
    directory.mkdir(parents=True, exist_ok=True)
    (root / "tmp").mkdir(exist_ok=True)
    payload: dict[str, object] = {"name": scenario}
    payload.update(extra)
    (directory / "scenario.json").write_text(json.dumps(payload), encoding="utf-8")
    write_launcher(root)
    return directory


async def run(
    root: Path,
    scenario: str,
    *,
    limits: OutputLimits | None = None,
    total: float = 20.0,
    reserve: float = 2.0,
    on_line: Callable[[bytes], None] | None = None,
    **extra: object,
) -> object:
    directory = workspace(root, scenario, **extra)
    return await run_bounded(
        arguments=[str(root / "codex")],
        environment=environment(root),
        working_directory=directory,
        stdin_payload=b'{"candidate":{}}',
        limits=limits or OutputLimits(),
        deadline=Deadline(total_seconds=total, cleanup_reserve_seconds=reserve),
        on_stdout_line=on_line or (lambda _line: None),
    )


async def group_is_gone(pgid_file: Path) -> bool:
    for _ in range(50):
        if pgid_file.exists():  # noqa: ASYNC240 - test-local polling, not runtime code
            break
        await asyncio.sleep(0.02)
    pgid = int(pgid_file.read_text(encoding="utf-8"))  # noqa: ASYNC240
    for _ in range(100):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.02)
    os.killpg(pgid, signal.SIGKILL)
    return False


async def test_a_clean_run_reports_a_complete_cleanup(tmp_path: Path) -> None:
    lines: list[bytes] = []
    result = await run(tmp_path, "success", on_line=lines.append)
    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert result.cleanup.reaped is True  # type: ignore[attr-defined]
    assert result.cleanup.complete is True  # type: ignore[attr-defined]
    assert any(b"turn.completed" in line for line in lines)


async def test_a_timeout_ends_the_process_group(tmp_path: Path) -> None:
    pgid_file = tmp_path / "pgid"
    with pytest.raises(ProcessError) as caught:
        await run(tmp_path, "hang", total=1.5, reserve=0.6, pgid_out=str(pgid_file))
    assert caught.value.failure is EvaluationFailure.DEADLINE_EXCEEDED
    assert caught.value.cleanup.signalled is True
    assert caught.value.cleanup.reaped is True
    assert await group_is_gone(pgid_file)


async def test_a_grandchild_in_the_group_is_ended_too(tmp_path: Path) -> None:
    pgid_file = tmp_path / "pgid"
    with pytest.raises(ProcessError):
        await run(tmp_path, "hang_with_grandchild", total=1.5, reserve=0.6, pgid_out=str(pgid_file))
    assert await group_is_gone(pgid_file)


async def test_outer_cancellation_cleans_up_and_is_re_raised(tmp_path: Path) -> None:
    pgid_file = tmp_path / "pgid"
    task = asyncio.create_task(run(tmp_path, "hang", total=30.0, pgid_out=str(pgid_file)))
    for _ in range(100):
        if pgid_file.exists():  # noqa: ASYNC240 - test-local polling, not runtime code
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await group_is_gone(pgid_file)


async def test_stdout_overflow_aborts_without_a_partial_result(tmp_path: Path) -> None:
    lines: list[bytes] = []
    with pytest.raises(ProcessError) as caught:
        await run(
            tmp_path,
            "stdout_flood",
            limits=OutputLimits(max_stdout_bytes=8192),
            on_line=lines.append,
        )
    assert caught.value.failure is EvaluationFailure.STDOUT_OVERFLOW
    assert caught.value.cleanup.reaped is True


async def test_stderr_overflow_is_its_own_failure(tmp_path: Path) -> None:
    with pytest.raises(ProcessError) as caught:
        await run(tmp_path, "stderr_flood", limits=OutputLimits(max_stderr_bytes=4096))
    assert caught.value.failure is EvaluationFailure.STDERR_OVERFLOW


async def test_one_endless_line_is_capped_separately(tmp_path: Path) -> None:
    with pytest.raises(ProcessError) as caught:
        await run(tmp_path, "long_line", limits=OutputLimits(max_line_bytes=4096))
    assert caught.value.failure is EvaluationFailure.LINE_OVERFLOW


async def test_a_failure_to_start_is_reported_as_such(tmp_path: Path) -> None:
    directory = workspace(tmp_path, "success")
    with pytest.raises(ProcessError) as caught:
        await run_bounded(
            arguments=[str(tmp_path / "does-not-exist")],
            environment=environment(tmp_path),
            working_directory=directory,
            stdin_payload=b"{}",
            limits=OutputLimits(),
            deadline=Deadline(total_seconds=5.0, cleanup_reserve_seconds=1.0),
            on_stdout_line=lambda _line: None,
        )
    assert caught.value.failure is EvaluationFailure.PROCESS_START_FAILED


async def test_a_consumer_rejection_stops_the_child_and_keeps_the_cause(
    tmp_path: Path,
) -> None:
    class Rejected(Exception):
        pass

    def reject(_line: bytes) -> None:
        raise Rejected("consumer said no")

    with pytest.raises(AbortedByConsumer) as caught:
        await run(tmp_path, "success", on_line=reject)
    assert isinstance(caught.value.cause, Rejected)
    assert caught.value.cleanup.reaped is True


async def test_no_time_left_means_no_process_is_started(tmp_path: Path) -> None:
    directory = workspace(tmp_path, "success")
    deadline = Deadline(total_seconds=0.2, cleanup_reserve_seconds=0.1)
    await asyncio.sleep(0.25)
    with pytest.raises(ProcessError) as caught:
        await run_bounded(
            arguments=[str(tmp_path / "codex")],
            environment=environment(tmp_path),
            working_directory=directory,
            stdin_payload=b"{}",
            limits=OutputLimits(),
            deadline=deadline,
            on_stdout_line=lambda _line: None,
        )
    assert caught.value.reason_code == "NO_TIME_TO_START"
