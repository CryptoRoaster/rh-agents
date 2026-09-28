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
    # Background throttles the Codex subprocess past its own deadlines.
    assert plist["ProcessType"] == "Standard"
    assert set(plist["EnvironmentVariables"]) == {
        "PATH",
        "RH_AGENTS_SCOUT_LOG_DIR",
        "RH_AGENTS_SCOUT_REQUIRE_BRANCH",
    }
    # The agent runs only a worktree on main.
    assert plist["EnvironmentVariables"]["RH_AGENTS_SCOUT_REQUIRE_BRANCH"] == "main"
    # A dry run installs nothing.
    assert not (tmp_path / "agents").exists()


def test_the_scripts_are_executable():
    for name in ("run-scout.sh", "install.sh", "uninstall.sh"):
        assert os.access(SCOUT / name, os.X_OK), name


def test_background_cannot_quietly_return_as_the_process_type():
    template = plistlib.loads((SCOUT / "com.rh-agents.scout.plist.template").read_text().encode())
    assert template["ProcessType"] == "Standard"
    assert "Nice" not in template and "LowPriorityIO" not in template


def test_the_scout_env_reaches_the_scout_process(tmp_path):
    """A provider-local override, such as the Codex timeout, is exported to the run."""
    uv = tmp_path / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "${{REASONING_TIMEOUT_SECONDS:-unset}} ${{REASONING_PROVIDER:-unset}}"'
        f' > "{tmp_path}/seen"\n'
    )
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    scout_env = tmp_path / "scout.env"
    scout_env.write_text("REASONING_PROVIDER=codex\nREASONING_TIMEOUT_SECONDS=120\n")
    subprocess.run(
        ["/bin/bash", str(SCOUT / "run-scout.sh")],
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "RH_AGENTS_UV": str(uv),
            "RH_AGENTS_SCOUT_LOG_DIR": str(tmp_path / "logs"),
            "RH_AGENTS_SCOUT_ENV": str(scout_env),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert (tmp_path / "seen").read_text().split() == ["120", "codex"]


def scratch_repo(tmp_path: Path, branch: str) -> Path:
    """A throwaway git repository carrying the scheduler scripts, on `branch`."""
    repo = tmp_path / "repo"
    (repo / "ops" / "scout").mkdir(parents=True)
    (repo / "backend").mkdir()
    for name in FILES:
        (repo / "ops" / "scout" / name).write_text((SCOUT / name).read_text())
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    for command in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(command, cwd=repo, env=env, check=True)
    if branch != "main":
        subprocess.run(["git", "switch", "-q", "-c", branch], cwd=repo, env=env, check=True)
    return repo


def run_guarded(tmp_path: Path, repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(repo / "ops" / "scout" / "run-scout.sh")],
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "RH_AGENTS_UV": str(fake_uv(tmp_path, 0)),
            "RH_AGENTS_SCOUT_LOG_DIR": str(tmp_path / "logs"),
            "RH_AGENTS_SCOUT_ENV": str(tmp_path / "absent.env"),
            "RH_AGENTS_SCOUT_REQUIRE_BRANCH": "main",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_the_scheduler_refuses_a_worktree_on_a_feature_branch(tmp_path):
    repo = scratch_repo(tmp_path, "feat/newer-orm")
    result = run_guarded(tmp_path, repo)
    assert result.returncode == 78
    assert "refused RUNTIME_NOT_ON_MAIN" in (tmp_path / "logs" / "scout.log").read_text()
    # The scout itself was never started.
    assert not (tmp_path / "args").exists()


def test_the_scheduler_refuses_a_modified_runtime_worktree(tmp_path):
    repo = scratch_repo(tmp_path, "main")
    (repo / "ops" / "scout" / "install.sh").write_text("# edited\n")
    result = run_guarded(tmp_path, repo)
    assert result.returncode == 78
    assert "refused RUNTIME_WORKTREE_MODIFIED" in (tmp_path / "logs" / "scout.log").read_text()


def test_the_scheduler_runs_a_clean_worktree_on_main(tmp_path):
    repo = scratch_repo(tmp_path, "main")
    # Untracked runtime files (.env, .venv) do not count as modifications.
    (repo / ".env").write_text("X=1\n")
    result = run_guarded(tmp_path, repo)
    assert result.returncode == 0
    assert (tmp_path / "args").read_text().split()[-1] == "--scout-once"


def test_the_agent_is_never_installed_from_a_feature_branch(tmp_path):
    repo = scratch_repo(tmp_path, "feat/anything")
    result = subprocess.run(
        ["/bin/bash", str(repo / "ops" / "scout" / "install.sh")],
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "RH_AGENTS_UV": str(fake_uv(tmp_path, 0)),
            "RH_AGENTS_LAUNCH_AGENTS": str(tmp_path / "agents"),
            "RH_AGENTS_SCOUT_LOG_DIR": str(tmp_path / "logs"),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert "refusing to install from branch feat/anything" in result.stderr
    assert not (tmp_path / "agents").exists()
