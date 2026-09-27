"""The launchd scout scheduler: what it runs, where, and what it never does.

Nothing here installs or loads a launchd job. The wrapper is executed against a
stand-in `uv` that records its arguments, so the exact command, working
directory and exit-code propagation are observed rather than read off a string.
"""

import os
import plistlib
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCOUT = REPO / "ops" / "scout"
FILES = ("run-scout.sh", "install.sh", "uninstall.sh", "com.rh-agents.scout.plist.template")


def fake_uv(tmp_path: Path, exit_code: int) -> Path:
    uv = tmp_path / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$PWD" > "{tmp_path}/cwd"\n'
        f'printf "%s\\n" "$@" > "{tmp_path}/args"\n'
        f"exit {exit_code}\n"
    )
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    return uv


def run_wrapper(tmp_path: Path, exit_code: int) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "RH_AGENTS_UV": str(fake_uv(tmp_path, exit_code)),
        "RH_AGENTS_SCOUT_LOG_DIR": str(tmp_path / "logs"),
        "RH_AGENTS_SCOUT_ENV": str(tmp_path / "absent.env"),
    }
    return subprocess.run(
        ["/bin/bash", str(SCOUT / "run-scout.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_the_wrapper_runs_the_scout_mode_from_the_backend(tmp_path):
    result = run_wrapper(tmp_path, 0)
    assert result.returncode == 0
    assert (tmp_path / "args").read_text().split() == [
        "run",
        "--locked",
        "python",
        "-m",
        "src.runner.main",
        "--scout-once",
    ]
    assert Path((tmp_path / "cwd").read_text().strip()).resolve() == (REPO / "backend").resolve()
    log = (tmp_path / "logs" / "scout.log").read_text()
    assert "scout: start" in log and "scout: exit 0" in log


def test_a_failed_run_keeps_its_exit_code_and_says_so(tmp_path):
    result = run_wrapper(tmp_path, 3)
    assert result.returncode == 3
    assert "scout: exit 3" in (tmp_path / "logs" / "scout.log").read_text()


def test_the_log_is_bounded(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "scout.log").write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    for index in (1, 2, 3):
        (logs / f"scout.log.{index}").write_text(f"old {index}")
    run_wrapper(tmp_path, 0)
    assert (logs / "scout.log").stat().st_size < 1024
    assert (logs / "scout.log.1").stat().st_size > 5 * 1024 * 1024
    assert (logs / "scout.log.2").read_text() == "old 1"
    assert not (logs / "scout.log.4").exists()


def test_nothing_but_the_scout_mode_is_ever_started():
    for name in FILES:
        text = (SCOUT / name).read_text()
        assert "--once" not in text.replace("--scout-once", ""), name
        assert "--preflight" not in text, name


def test_no_secret_is_committed_in_the_scheduler():
    for name in FILES:
        text = (SCOUT / name).read_text().lower()
        for marker in ("sk-ant", "api_key=", "apikey", "password", "secret="):
            assert marker not in text, (name, marker)


def test_the_rendered_plist_runs_the_wrapper_every_fifteen_minutes(tmp_path):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "RH_AGENTS_UV": str(fake_uv(tmp_path, 0)),
        "RH_AGENTS_LAUNCH_AGENTS": str(tmp_path / "agents"),
        "RH_AGENTS_SCOUT_LOG_DIR": str(tmp_path / "logs"),
    }
    result = subprocess.run(
        ["/bin/bash", str(SCOUT / "install.sh"), "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    plist = plistlib.loads(result.stdout.encode())
    assert plist["Label"] == "com.rh-agents.scout"
    assert plist["ProgramArguments"] == ["/bin/bash", f"{REPO}/ops/scout/run-scout.sh"]
    assert plist["WorkingDirectory"] == f"{REPO}/backend"
    assert plist["StartInterval"] == 900
    assert plist["RunAtLoad"] is False
    assert set(plist["EnvironmentVariables"]) == {"PATH", "RH_AGENTS_SCOUT_LOG_DIR"}
    # A dry run installs nothing.
    assert not (tmp_path / "agents").exists()


def test_the_scripts_are_executable():
    for name in ("run-scout.sh", "install.sh", "uninstall.sh"):
        assert os.access(SCOUT / name, os.X_OK), name
