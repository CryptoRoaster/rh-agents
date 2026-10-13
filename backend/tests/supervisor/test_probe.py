"""Read-only gathering: launchd's answer and the database's facts.

launchd is a stand-in script here; the database is the disposable schema the
risk-request tests use (PostgreSQL under `TEST_DATABASE_URL`, SQLite without).
"""

import stat
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select

from src.data.tables import PaperJobRunRow, PositionRow
from src.orchestration.entryhalt.service import EntryHaltService
from src.supervisor.probe import database_facts, parse_service, service_status
from tests.jobruns.conftest import exit_run
from tests.riskrequest.conftest import risk_db  # noqa: F401

NOW = datetime(2026, 10, 13, 18, tzinfo=UTC)

LOADED = """gui/501/com.rh-agents.exit-supervisor = {
	active count = 1
	state = running
	pid = 4242
	last exit code = 0
}
"""


def fake_launchctl(tmp_path, *, code: int, printed: str = "", error: str = ""):
    script = tmp_path / "launchctl"
    (tmp_path / "out").write_text(printed)
    (tmp_path / "err").write_text(error)
    script.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" >> "{tmp_path}/calls"\n'
        f'cat "{tmp_path}/out"; cat "{tmp_path}/err" >&2\n'
        f"exit {code}\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_a_running_job_is_read_as_running(tmp_path):
    status = service_status(launchctl=fake_launchctl(tmp_path, code=0, printed=LOADED), uid=501)
    assert status.loaded and status.running and status.last_exit_status == 0
    assert (tmp_path / "calls").read_text().split() == [
        "print",
        "gui/501/com.rh-agents.exit-supervisor",
    ]


def test_an_unknown_label_is_not_loaded(tmp_path):
    launchctl = fake_launchctl(tmp_path, code=113, error="Could not find service")
    assert service_status(launchctl=launchctl, uid=501).loaded is False


def test_an_unanswerable_launchctl_is_unknown(tmp_path):
    assert service_status(launchctl=fake_launchctl(tmp_path, code=5), uid=501).loaded is None
    assert service_status(launchctl=str(tmp_path / "absent"), uid=501).loaded is None


def test_a_waiting_job_with_a_failed_last_tick():
    status = parse_service("\tstate = not running\n\tlast exit code = 1\n")
    assert status.loaded and not status.running and status.last_exit_status == 1


async def facts(sessions, now=NOW):
    return await database_facts(
        sessions, now=now, exit_run_max_seconds=60, entry_run_max_seconds=300
    )


async def test_an_empty_book_with_no_exit_runs(risk_db):  # noqa: F811
    _, sessions = risk_db
    found = await facts(sessions)
    assert found.coverage.total == 0
    assert found.evidence == [] and found.last_exit_run is None
    assert found.last_ready_finished_at is None
    assert not found.entry_blocked
    assert not found.exit_run_active and not found.entry_run_active


async def test_open_positions_halts_and_the_latest_ready_run(risk_db):  # noqa: F811
    _, sessions = risk_db
    await exit_run(sessions, NOW - timedelta(seconds=100))
    await exit_run(sessions, NOW - timedelta(seconds=40), ready=False, outcome="SUCCEEDED")
    async with sessions.begin() as session:
        session.add(
            PositionRow(
                id=uuid4(),
                asset_id="bsc:mainnet:0x" + "ab" * 20,
                quantity=Decimal("1"),
                cost_basis_usd=Decimal("10"),
                realized_pnl_usd=Decimal("0"),
                created_at=NOW,
                updated_at=NOW,
                source="test",
                correlation_id=uuid4(),
            )
        )
    # Through the operator's own control, which writes its audit event.
    await EntryHaltService(sessions, kill_switch=False).set_manual(reason="TEST", actor=None)
    found = await facts(sessions)
    # No case-bound entry opened it: no exit policy closes it.
    assert found.coverage.unknown == 1 and found.coverage.total == 1
    assert found.entry_blocked and found.entry_halt_manual
    assert found.last_ready_finished_at == NOW - timedelta(seconds=100)
    assert found.last_exit_run["exit_ready"] is False
    assert len(found.evidence) == 2


async def test_only_young_unfinished_runs_are_active(risk_db):  # noqa: F811
    _, sessions = risk_db
    await exit_run(sessions, None, started=NOW - timedelta(seconds=200))
    assert not (await facts(sessions)).exit_run_active
    await exit_run(sessions, None, started=NOW - timedelta(seconds=20))
    assert (await facts(sessions)).exit_run_active
    await exit_run(
        sessions,
        None,
        started=NOW - timedelta(seconds=20),
        job="PAPER_ENTRY_JOB",
        mode="ENTRIES_ONLY",
    )
    assert (await facts(sessions)).entry_run_active


async def test_reading_writes_nothing(risk_db):  # noqa: F811
    _, sessions = risk_db
    await exit_run(sessions, NOW - timedelta(seconds=30))

    async def count():
        async with sessions() as session:
            return await session.scalar(select(func.count()).select_from(PaperJobRunRow))

    before = await count()
    for _ in range(3):
        await facts(sessions)
    assert await count() == before
