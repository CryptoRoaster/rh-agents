"""The index job's command line: an interrupt is a named failure, never a traceback.

systemd stops the index unit with SIGINT. Every window is written in one
transaction, so nothing is half-stored; the run still has to say what
happened, in one JSON line, like every other outcome of the job.
"""

import json
import sys

import pytest

from src.agents.atlas.v4 import index_job


def test_an_interrupted_index_run_is_reported_by_name(monkeypatch, capsys) -> None:
    def interrupted(coroutine):
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(index_job.asyncio, "run", interrupted)
    monkeypatch.setattr(sys, "argv", ["index_job", "--chain", "robinhood", "--max-requests", "5"])

    with pytest.raises(SystemExit) as stopped:
        index_job.main()

    assert stopped.value.code == index_job.FAILED
    assert json.loads(capsys.readouterr().out) == {
        "status": "FAILED",
        "reason": "INDEX_INTERRUPTED",
    }
