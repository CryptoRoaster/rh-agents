"""The migration rehearsal script refuses everything that could touch the source or lose a backup.

`psql`, `pg_dump`, `pg_restore`, `createdb` and `uv` are stand-ins that record
their arguments. The real rehearsal (backup, restore, 0025/0026, schema check)
was run against a scratch PostgreSQL database; see docs/runtime-host-readiness.md.
"""

import subprocess
from pathlib import Path

import pytest

from tests.ops.test_exit_supervisor_linux import calls, executable

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "ops" / "db" / "rehearse-migration.sh"


def tools(tmp_path: Path, *, server: int = 170011, dump_version: str = "17.2", exists: str = "0"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable(
        bin_dir / "psql",
        f'echo "$*" >> "{tmp_path}/psql.calls"\n'
        f'case "$*" in *server_version_num*) echo {server} ;; *pg_database*) echo {exists} ;;'
        " *paper_accounts*) echo 1 ;; *) echo 0 ;; esac\n",
    )
    executable(
        bin_dir / "pg_dump",
        f'echo "$*" >> "{tmp_path}/pg_dump.calls"\n'
        f'[[ "$1" == --version ]] && {{ echo "pg_dump (PostgreSQL) {dump_version}"; exit 0; }}\n'
        'while (($#)); do [[ "$1" == -f ]] && { echo dump > "$2"; }; shift; done\n',
    )
    executable(
        bin_dir / "pg_restore",
        f'echo "$*" >> "{tmp_path}/pg_restore.calls"\necho "1; TABLE DATA x"\n',
    )
    executable(bin_dir / "createdb", f'echo "$*" >> "{tmp_path}/createdb.calls"\n')
    executable(
        tmp_path / "uv",
        f'echo "$*" >> "{tmp_path}/uv.calls"\n'
        'case "$*" in *current*|*heads*) echo "0026 (head)" ;; esac\n',
    )
    return bin_dir, {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "RH_AGENTS_UV": str(tmp_path / "uv"),
        "PGHOST": "localhost",
        "PGUSER": "tester",
    }


def rehearse(env, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30
    )


def arguments(tmp_path: Path, bin_dir: Path, **overrides: str) -> list[str]:
    values = {
        "--source-db": "rh_agents",
        "--scratch-db": "rh_agents_rehearsal_1",
        "--out": str(tmp_path / "backup.dump"),
        "--pg-bin": str(bin_dir),
    } | overrides
    return [item for pair in values.items() for item in pair]


def test_a_rehearsal_backs_up_restores_and_migrates_the_scratch_copy_only(tmp_path):
    bin_dir, env = tools(tmp_path)
    result = rehearse(env, *arguments(tmp_path, bin_dir))
    assert result.returncode == 0, result.stderr
    assert "REHEARSAL_PASSED" in result.stdout
    assert calls(tmp_path, "createdb") == ["rh_agents_rehearsal_1"]
    assert all(
        "-d rh_agents_rehearsal_1" in call
        for call in calls(tmp_path, "pg_restore")
        if "-l" not in call
    )
    # The source is only ever dumped; every migration targets the scratch copy.
    assert [call for call in calls(tmp_path, "pg_dump") if "--version" not in call] == [
        f"-Fc --no-owner -d rh_agents -f {tmp_path / 'backup.dump'}"
    ]
    assert "upgrade head" in "\n".join(calls(tmp_path, "uv"))
    assert (tmp_path / "backup.dump").stat().st_mode & 0o077 == 0
    assert "postgresql+asyncpg" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "overrides",
    [
        {"--scratch-db": "rh_agents"},
        {"--scratch-db": "rh_agents_copy"},
        {"--scratch-db": "Rehearsal; drop"},
        {"--source-db": "rh agents"},
        {"--out": ""},
    ],
    ids=["scratch-is-source", "scratch-not-named-so", "unsafe-scratch", "unsafe-source", "no-out"],
)
def test_unsafe_arguments_are_refused_before_any_tool_runs(tmp_path, overrides):
    bin_dir, env = tools(tmp_path)
    result = rehearse(env, *arguments(tmp_path, bin_dir, **overrides))
    assert result.returncode == 2
    assert calls(tmp_path, "pg_dump") == calls(tmp_path, "createdb") == []


def test_an_existing_backup_file_is_never_overwritten(tmp_path):
    bin_dir, env = tools(tmp_path)
    (tmp_path / "backup.dump").write_text("earlier backup\n")
    assert rehearse(env, *arguments(tmp_path, bin_dir)).returncode == 2
    assert (tmp_path / "backup.dump").read_text() == "earlier backup\n"


def test_a_pg_dump_older_than_the_server_is_refused(tmp_path):
    bin_dir, env = tools(tmp_path, dump_version="16.14")
    result = rehearse(env, *arguments(tmp_path, bin_dir))
    assert result.returncode == 2
    assert "does not match server 17" in result.stderr
    assert not (tmp_path / "backup.dump").exists()


def test_an_existing_scratch_database_is_never_reused(tmp_path):
    bin_dir, env = tools(tmp_path, exists="1")
    assert rehearse(env, *arguments(tmp_path, bin_dir)).returncode == 2
    assert calls(tmp_path, "createdb") == []
    assert not (tmp_path / "backup.dump").exists()
