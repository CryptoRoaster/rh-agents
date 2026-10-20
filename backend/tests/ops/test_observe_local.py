"""The local observation scripts (macOS): what they run, what they refuse, how they stop.

Nothing here loads a launchd agent, reaches a network or touches a database.
The scripts run from a throwaway git checkout holding a copy of ops/observe,
against a stand-in Python that records its arguments and environment, a
stand-in launchctl and a stand-in psql.
"""

import os
import plistlib
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
OBSERVE = REPO / "ops" / "observe"
FILES = (
    "common.sh",
    "run-observe.sh",
    "start.sh",
    "stop.sh",
    "status.sh",
    "observe.env.example",
    "com.rh-agents.observe.plist.template",
)
PROFILE = (OBSERVE / "observe.env.example").read_text().replace("YOUR_ROLE", "observer")
PSQL_EXAMPLE = "/opt/homebrew/opt/postgresql@17/bin/psql"
assert PSQL_EXAMPLE in PROFILE


def base_profile(box: dict[str, Path]) -> str:
    """The example profile, with psql pointing at the box's stand-in."""
    return PROFILE.replace(PSQL_EXAMPLE, str(box["bin"] / "psql"))


def executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def box(tmp_path: Path) -> dict[str, Path]:
    checkout = tmp_path / "checkout"
    shutil.copytree(OBSERVE, checkout / "ops" / "observe")
    (checkout / "backend").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Only this throwaway copy uses stand-ins for launchctl and plutil; no
    # variable can swap them in the real scripts. Never reach the real ones.
    common = checkout / "ops" / "observe" / "common.sh"
    text = common.read_text()
    for real, name in (("/bin/launchctl", "LAUNCHCTL"), ("/usr/bin/plutil", "PLUTIL")):
        assert text.count(f"{name}={real}\n") == 1
        text = text.replace(f"{name}={real}\n", f"{name}={bin_dir / name.lower()}\n")
    common.write_text(text)
    git = ["git", "-C", str(checkout), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "observe"], check=True)
    # The commit counts as merged unless a test says otherwise.
    subprocess.run([*git, "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
    record = tmp_path / "record"
    record.mkdir()
    # The checkout's own virtualenv (untracked, like the real one).
    (checkout / "backend" / ".venv" / "bin").mkdir(parents=True)
    python = executable(
        checkout / "backend" / ".venv" / "bin" / "python",
        f'echo "$PWD" > "{record}/cwd"\n'
        f'printf "%s\\n" "$@" > "{record}/args"\n'
        f'env | sort > "{record}/env"\n'
        f'if [[ -e "{record}/sleep" ]]; then\n'
        f"  trap 'echo terminated > \"{record}/signal\"; exit 143' TERM\n"
        f'  touch "{record}/started"\n'
        "  for _ in $(seq 1 100); do sleep 0.1; done\n"
        "fi\n"
        f'exit "$(cat "{record}/exit" 2>/dev/null || echo 0)"\n',
    )
    executable(
        bin_dir / "launchctl",
        f'printf "%s\\n" "$*" >> "{record}/launchctl"\n'
        f'[[ "$1" != print || -e "{record}/loaded" ]]\n',
    )
    env_file = tmp_path / "observe.env"
    env_file.write_text(PROFILE.replace(PSQL_EXAMPLE, str(bin_dir / "psql")))
    return {
        "tmp": tmp_path,
        "checkout": checkout,
        "bin": bin_dir,
        "record": record,
        "python": python,
        "env_file": env_file,
        "state": tmp_path / "state",
        "logs": tmp_path / "logs",
    }


def environment(box: dict[str, Path], **extra: str) -> dict[str, str]:
    return {
        "PATH": f"{box['bin']}:/usr/bin:/bin",
        "HOME": str(box["tmp"]),
        "RH_AGENTS_OBSERVE_ENV": str(box["env_file"]),
        "RH_AGENTS_OBSERVE_STATE_DIR": str(box["state"]),
        "RH_AGENTS_OBSERVE_LOG_DIR": str(box["logs"]),
        **extra,
    }


def script(box: dict[str, Path], name: str, *args: str, **extra: str):
    return subprocess.run(
        ["/bin/bash", str(box["checkout"] / "ops" / "observe" / name), *args],
        env=environment(box, **extra),
        capture_output=True,
        text=True,
        timeout=60,
    )


def profile_with(box: dict[str, Path], **values: str) -> None:
    lines = [line for line in base_profile(box).splitlines() if line.split("=", 1)[0] not in values]
    lines += [f"{key}={value}" for key, value in values.items()]
    box["env_file"].write_text("\n".join(lines) + "\n")


def log(box: dict[str, Path]) -> str:
    return (box["logs"] / "observe.log").read_text()


# ------------------------------------------------------------ one run


def test_a_run_starts_only_the_scout_mode_from_the_backend(box):
    result = script(box, "run-observe.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (box["record"] / "args").read_text().split() == ["-m", "src.runner.main", "--scout-once"]
    cwd = Path((box["record"] / "cwd").read_text().strip()).resolve()
    assert cwd == (box["checkout"] / "backend").resolve()
    assert "observe: start (database rh_agents_observe" in log(box)
    assert "observe: exit 0" in log(box)
    assert not (box["state"] / "run.lock").exists()


def test_the_scout_sees_the_profile_and_nothing_inherited(box):
    script(
        box,
        "run-observe.sh",
        PAPER_RUNNER_ENABLED="true",
        TRADING_MODE="PAPER",
        ANTHROPIC_API_KEY="inherited",
        RH_RPC_HTTP_URL="https://inherited.example",
    )
    seen = dict(
        line.split("=", 1)
        for line in (box["record"] / "env").read_text().splitlines()
        if "=" in line
    )
    assert seen["TRADING_MODE"] == "OBSERVE"
    assert seen["PAPER_RUNNER_ENABLED"] == "false"
    assert seen["EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_DAY"] == "0"
    assert "ANTHROPIC_API_KEY" not in seen
    assert "RH_RPC_HTTP_URL" not in seen
    assert not [key for key in seen if key.startswith("RH_AGENTS_")]


def test_a_failed_run_keeps_its_exit_code(box):
    (box["record"] / "exit").write_text("3")
    result = script(box, "run-observe.sh")
    assert result.returncode == 3
    assert "observe: exit 3" in log(box)


def test_check_validates_and_runs_nothing(box):
    result = script(box, "run-observe.sh", "--check")
    assert result.returncode == 0
    assert "profile ok (database rh_agents_observe" in result.stdout
    assert not (box["record"] / "args").exists()


# ------------------------------------------------------------ refusals


@pytest.mark.parametrize(
    ("values", "reason"),
    [
        ({"TRADING_MODE": "PAPER"}, "OBSERVE_REQUIRES_TRADING_MODE"),
        ({"PAPER_RUNNER_ENABLED": "true"}, "OBSERVE_REQUIRES_PAPER_RUNNER_ENABLED"),
        ({"EARLY_SCOUT_ENABLED": "false"}, "OBSERVE_REQUIRES_EARLY_SCOUT_ENABLED"),
        ({"MARKET_CHAINS": "robinhood,bsc"}, "OBSERVE_REQUIRES_MARKET_CHAINS"),
        ({"REASONING_PROVIDER": "anthropic"}, "OBSERVE_REQUIRES_REASONING_PROVIDER"),
        (
            {"DATABASE_URL": "postgresql+asyncpg://observer@localhost/rh_agents"},
            "OBSERVE_DATABASE_NOT_ISOLATED",
        ),
        (
            {"DATABASE_URL": "postgresql+asyncpg://observer@localhost/rh_agents_test"},
            "OBSERVE_DATABASE_NOT_ISOLATED",
        ),
        (
            {"DATABASE_URL": "postgresql+asyncpg://observer:pw@localhost/rh_agents_observe"},
            "OBSERVE_DATABASE_NOT_ISOLATED",
        ),
        (
            {"DATABASE_URL": "postgresql+asyncpg://observer@db.example.com/rh_agents_observe"},
            "OBSERVE_DATABASE_NOT_ISOLATED",
        ),
        ({"EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_RUN": "1"}, "OBSERVE_MODEL_BUDGET_NOT_APPROVED"),
        ({"EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_DAY": "96"}, "OBSERVE_MODEL_BUDGET_NOT_APPROVED"),
        (
            {"COMMANDER_INTAKE_ENABLED": "true"},
            "OBSERVE_ENV_KEY_NOT_ALLOWED_COMMANDER_INTAKE_ENABLED",
        ),
        ({"PRE_VECTOR_EARLY_ENTRY_ENABLED": "true"}, "OBSERVE_ENV_KEY_NOT_ALLOWED_PRE_VECTOR"),
        ({"EVM_RUNTIME_ENABLED": "true"}, "OBSERVE_ENV_KEY_NOT_ALLOWED_EVM_RUNTIME_ENABLED"),
        ({"ANTHROPIC_API_KEY": "sk"}, "OBSERVE_ENV_KEY_NOT_ALLOWED_ANTHROPIC_API_KEY"),
    ],
)
def test_a_profile_that_could_trade_or_leave_its_database_is_refused(box, values, reason):
    profile_with(box, **values)
    result = script(box, "run-observe.sh")
    assert result.returncode == 78
    assert reason in log(box)
    assert not (box["record"] / "args").exists()


@pytest.mark.parametrize(
    ("values", "reason"),
    [
        (
            {"EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_RUN": "5"},
            "OBSERVE_MODEL_BUDGET_ABOVE_CAP",
        ),
        (
            {"EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_DAY": "97"},
            "OBSERVE_MODEL_BUDGET_ABOVE_CAP",
        ),
        ({"EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_DAY": ""}, "OBSERVE_MODEL_BUDGET_NOT_EXPLICIT"),
    ],
)
def test_an_approved_model_budget_is_explicit_and_capped(box, values, reason):
    profile_with(box, RH_AGENTS_OBSERVE_MODEL_BUDGET_APPROVED="yes", **values)
    assert script(box, "run-observe.sh").returncode == 78
    assert reason in log(box)


def test_an_approved_model_budget_allows_orbit_reviews(box):
    profile_with(
        box,
        EARLY_SCOUT_MAX_ORBIT_REVIEWS_PER_RUN="2",
        RH_AGENTS_OBSERVE_MODEL_BUDGET_APPROVED="yes",
    )
    assert script(box, "run-observe.sh").returncode == 0


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("TRADING_MODE=OBSERVE\nTRADING_MODE=OBSERVE\n", "OBSERVE_ENV_KEY_REPEATED_TRADING_MODE"),
        ("CODEX_EXECUTABLE=$(touch MARKER)\n", "OBSERVE_ENV_MALFORMED"),
        ('TRADING_MODE="OBSERVE"\n', "OBSERVE_ENV_MALFORMED"),
        ("export TRADING_MODE=OBSERVE\n", "OBSERVE_ENV_MALFORMED"),
    ],
)
def test_the_env_file_is_read_never_sourced(box, text, reason):
    marker = box["tmp"] / "MARKER"
    box["env_file"].write_text(text.replace("MARKER", str(marker)) + base_profile(box))
    result = script(box, "run-observe.sh")
    assert result.returncode == 78
    assert reason in log(box)
    assert not marker.exists()


def test_a_missing_profile_is_refused(box):
    box["env_file"].unlink()
    assert script(box, "run-observe.sh").returncode == 78
    assert "OBSERVE_ENV_MISSING" in log(box)


def test_a_repository_env_file_is_refused(box):
    (box["checkout"] / ".env").write_text("PAPER_RUNNER_ENABLED=true\n")
    assert script(box, "run-observe.sh").returncode == 78
    assert "OBSERVE_REPOSITORY_ENV_PRESENT" in log(box)


def test_a_dangling_repository_env_link_is_refused(box):
    (box["checkout"] / ".env").symlink_to(box["tmp"] / "nowhere")
    assert script(box, "run-observe.sh").returncode == 78
    assert "OBSERVE_REPOSITORY_ENV_PRESENT" in log(box)


def test_an_untracked_module_next_to_the_backend_is_refused(box):
    (box["checkout"] / "backend" / "src.py").write_text("raise SystemExit\n")
    assert script(box, "run-observe.sh").returncode == 78
    assert "OBSERVE_WORKTREE_MODIFIED" in log(box)


def test_a_modified_checkout_is_refused(box):
    with (box["checkout"] / "ops" / "observe" / "observe.env.example").open("a") as handle:
        handle.write("# changed\n")
    assert script(box, "run-observe.sh").returncode == 78
    assert "OBSERVE_WORKTREE_MODIFIED" in log(box)


def test_an_uncommitted_file_in_the_code_is_refused(box):
    (box["checkout"] / "ops" / "observe" / "extra.sh").write_text("echo\n")
    assert script(box, "run-observe.sh").returncode == 78
    assert "OBSERVE_WORKTREE_MODIFIED" in log(box)
    result = script(box, "start.sh", "--confirm")
    assert result.returncode == 78
    assert "OBSERVE_WORKTREE_MODIFIED" in result.stderr
    assert not (box["record"] / "launchctl").exists()


# Finder's buddy-allocator header: the first eight bytes of every .DS_Store.
FINDER_MAGIC = b"\x00\x00\x00\x01Bud1"


def finder_file(path: Path, body: bytes = FINDER_MAGIC + b"\x00" * 4088) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


@pytest.mark.parametrize("where", ["backend", "backend/src", "ops/observe"])
def test_finder_metadata_in_the_code_does_not_block_a_run(box, where):
    finder_file(box["checkout"] / where / ".DS_Store")
    assert script(box, "run-observe.sh").returncode == 0
    assert "OBSERVE_WORKTREE_MODIFIED" not in log(box)


def test_finder_metadata_that_git_ignores_does_not_block_a_run(box):
    git = ["git", "-C", str(box["checkout"]), "-c", "user.name=t", "-c", "user.email=t@t"]
    (box["checkout"] / ".gitignore").write_text(".DS_Store\n")
    subprocess.run([*git, "add", ".gitignore"], check=True)
    subprocess.run([*git, "commit", "-qm", "ignore"], check=True)
    subprocess.run([*git, "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
    finder_file(box["checkout"] / "backend" / ".DS_Store")
    assert script(box, "run-observe.sh").returncode == 0


def test_an_ignored_directory_under_finder_name_is_refused(box):
    git = ["git", "-C", str(box["checkout"]), "-c", "user.name=t", "-c", "user.email=t@t"]
    (box["checkout"] / ".gitignore").write_text(".DS_Store\n")
    subprocess.run([*git, "add", ".gitignore"], check=True)
    subprocess.run([*git, "commit", "-qm", "ignore"], check=True)
    subprocess.run([*git, "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
    finder_file(box["checkout"] / "backend" / ".DS_Store" / "evil.py", FINDER_MAGIC)
    assert script(box, "run-observe.sh").returncode == 78
    assert "OBSERVE_WORKTREE_MODIFIED" in log(box)


def test_a_finder_file_still_leaves_a_real_module_refused(box):
    finder_file(box["checkout"] / "backend" / ".DS_Store")
    (box["checkout"] / "backend" / "sitecustomize.py").write_text("raise SystemExit\n")
    assert script(box, "run-observe.sh").returncode == 78
    assert "OBSERVE_WORKTREE_MODIFIED" in log(box)


@pytest.mark.parametrize(
    "make",
    [
        # Code under Finder's name: not Finder's format.
        lambda root: (root / "backend" / ".DS_Store").write_text("import os\n"),
        # Empty: Finder never writes one.
        lambda root: (root / "backend" / ".DS_Store").write_bytes(b""),
        # Finder's header, but a Python name.
        lambda root: finder_file(root / "backend" / ".DS_Store.py"),
        lambda root: finder_file(root / "backend" / "x.DS_Store"),
        # A directory of that name hides whatever it holds.
        lambda root: finder_file(root / "backend" / ".DS_Store" / "evil.py", b"import os\n"),
        # A link points anywhere.
        lambda root: (root / "backend" / ".DS_Store").symlink_to(
            root / "ops" / "observe" / "common.sh"
        ),
        # A .pth file under Finder's header is still code to site.
        lambda root: finder_file(root / "backend" / "finder.pth"),
    ],
    ids=["code", "empty", "py-suffix", "other-name", "directory", "symlink", "pth"],
)
def test_anything_but_finder_metadata_under_that_name_is_refused(box, make):
    make(box["checkout"])
    assert script(box, "run-observe.sh").returncode == 78
    assert "OBSERVE_WORKTREE_MODIFIED" in log(box)


@pytest.mark.parametrize("failing", ["1", "2"])
def test_a_failing_git_status_never_passes_the_code_check(box, failing):
    """Whichever `git status` call fails, the check fails: no listing is no proof."""
    (box["checkout"] / "backend" / "evil.py").write_text("import os\n")
    common = box["checkout"] / "ops" / "observe" / "common.sh"
    probe = (
        f'source "{common}"\n'
        "calls=0\n"
        "git() {\n"
        '  if [[ " $* " == *" status "* ]]; then\n'
        "    calls=$((calls + 1))\n"
        f"    [[ $calls != {failing} ]] || return 128\n"
        "  fi\n"
        '  command git "$@"\n'
        "}\n"
        "observe_code_clean && echo CLEAN || echo REFUSED\n"
    )
    result = subprocess.run(
        ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin:/usr/sbin:/sbin", "/bin/bash", "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.stdout.strip().splitlines()[-1] == "REFUSED", result.stderr


def test_a_tracked_file_replaced_by_finder_bytes_is_still_modified(box):
    target = box["checkout"] / "ops" / "observe" / "observe.env.example"
    target.write_bytes(FINDER_MAGIC)
    assert script(box, "run-observe.sh").returncode == 78
    assert "OBSERVE_WORKTREE_MODIFIED" in log(box)


def test_start_accepts_finder_metadata_and_still_refuses_code(box):
    finder_file(box["checkout"] / "backend" / ".DS_Store")
    (box["checkout"] / "ops" / "observe" / "extra.sh").write_text("echo\n")
    result = script(box, "start.sh", "--confirm")
    assert result.returncode == 78
    assert "OBSERVE_WORKTREE_MODIFIED" in result.stderr


# ------------------------------------------------------------ bytecode and git hooks


def test_the_scout_reads_no_bytecode_from_the_checkout(box):
    """A fresh, empty bytecode prefix per run: nothing in __pycache__ is ever loaded."""
    assert script(box, "run-observe.sh").returncode == 0
    env = dict(line.split("=", 1) for line in (box["record"] / "env").read_text().splitlines())
    prefix = Path(env["PYTHONPYCACHEPREFIX"])
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert prefix.is_relative_to(box["state"])
    # Removed again once the run is over.
    assert not prefix.exists()


def test_the_preflight_reads_no_bytecode_from_the_checkout(box):
    assert script(box, "run-observe.sh", "--preflight").returncode == 0
    env = (box["record"] / "env").read_text()
    assert "PYTHONPYCACHEPREFIX=" in env
    assert "PYTHONDONTWRITEBYTECODE=1" in env


def test_a_planted_unchecked_hash_pyc_is_ignored_under_the_prefix(tmp_path):
    """The Python behaviour the prefix relies on, with the interpreter itself."""
    import importlib.util
    import py_compile
    import sys

    (tmp_path / "gen").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / "gen" / "mod.py").write_text("VALUE = 'planted'\n")
    (tmp_path / "app" / "mod.py").write_text("VALUE = 'source'\n")
    cached = importlib.util.cache_from_source(str(tmp_path / "app" / "mod.py"))
    py_compile.compile(
        str(tmp_path / "gen" / "mod.py"),
        cfile=cached,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )

    def value(**extra: str) -> str:
        return subprocess.run(
            [sys.executable, "-c", "import mod; print(mod.VALUE)"],
            cwd=tmp_path / "app",
            env={"PATH": "/usr/bin:/bin", **extra},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    assert value() == "planted"
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert value(PYTHONPYCACHEPREFIX=str(fresh), PYTHONDONTWRITEBYTECODE="1") == "source"


def test_a_configured_fsmonitor_is_never_asked(box):
    """git status could be told by a monitor hook that nothing changed."""
    marker = box["tmp"] / "fsmonitor-called"
    hook = executable(box["tmp"] / "fsmonitor", f'touch "{marker}"\nexit 0\n')
    git = ["git", "-C", str(box["checkout"])]
    subprocess.run([*git, "config", "core.fsmonitor", str(hook)], check=True)
    (box["checkout"] / "backend" / "evil.py").write_text("import os\n")
    assert script(box, "run-observe.sh").returncode == 78
    assert "OBSERVE_WORKTREE_MODIFIED" in log(box)
    assert not marker.exists()


def commit_module(box, name: str = "mod.py", text: str = "VALUE = 'source'\n") -> Path:
    """A tracked backend module, committed and merged."""
    git = ["git", "-C", str(box["checkout"]), "-c", "user.name=t", "-c", "user.email=t@t"]
    path = box["checkout"] / "backend" / name
    path.write_text(text)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "module"], check=True)
    subprocess.run([*git, "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
    return path


def same_size_edit(path: Path) -> None:
    """Overwrite with content of the same size and put the old mtime back."""
    stat_before = path.stat()
    path.write_text(path.read_text().replace("source", "evil!!"))
    os.utime(path, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))


@pytest.mark.parametrize("setting", [("core.trustctime", "false"), ("core.checkStat", "minimal")])
def test_a_stat_setting_never_hides_a_same_size_edit(box, setting):
    module = commit_module(box)
    subprocess.run(["git", "-C", str(box["checkout"]), "config", *setting], check=True)
    same_size_edit(module)
    assert script(box, "run-observe.sh").returncode == 78
    assert "OBSERVE_WORKTREE_MODIFIED" in log(box)


def test_a_configured_work_tree_never_points_the_gate_elsewhere(box):
    commit_module(box)
    clean = box["tmp"] / "clean"
    shutil.copytree(box["checkout"], clean, ignore=shutil.ignore_patterns(".git"))
    subprocess.run(
        ["git", "-C", str(box["checkout"]), "config", "core.worktree", str(clean)], check=True
    )
    (box["checkout"] / "backend" / "evil.py").write_text("import os\n")
    assert script(box, "run-observe.sh").returncode == 78
    assert "OBSERVE_WORKTREE_MODIFIED" in log(box)


def test_a_clean_filter_never_hides_an_edit_and_never_runs(box):
    module = commit_module(box)
    marker = box["tmp"] / "filter-ran"
    original = box["tmp"] / "original.py"
    original.write_text(module.read_text())
    git = ["git", "-C", str(box["checkout"])]
    subprocess.run(
        [*git, "config", "filter.hide.clean", f'touch "{marker}"; cat "{original}"'], check=True
    )
    attributes = box["checkout"] / ".git" / "info" / "attributes"
    attributes.parent.mkdir(parents=True, exist_ok=True)
    attributes.write_text("*.py filter=hide\n")
    module.write_text("import os  # changed\n")
    assert script(box, "run-observe.sh").returncode == 78
    assert "OBSERVE_WORKTREE_MODIFIED" in log(box)


def test_a_global_git_config_is_never_read(box):
    module = commit_module(box)
    (box["tmp"] / ".gitconfig").write_text("[core]\n\ttrustctime = false\n\tcheckStat = minimal\n")
    same_size_edit(module)
    assert script(box, "run-observe.sh").returncode == 78


def test_a_tracked_file_that_is_missing_is_refused(box):
    commit_module(box).unlink()
    assert script(box, "run-observe.sh").returncode == 78
    assert "OBSERVE_WORKTREE_MODIFIED" in log(box)


def test_the_bytecode_prefix_is_removed_without_recursive_deletion(box):
    """Left empty by PYTHONDONTWRITEBYTECODE, so rmdir suffices; a filled one is kept."""
    text = (box["checkout"] / "ops" / "observe" / "common.sh").read_text()
    assert "rm -rf" not in text
    assert script(box, "run-observe.sh").returncode == 0
    assert not list(box["state"].glob("pycache.*"))


def test_a_stale_bytecode_prefix_from_a_killed_run_is_cleared_when_empty(box):
    box["state"].mkdir(parents=True, exist_ok=True)
    stale = box["state"] / "pycache.ABC123"
    stale.mkdir()
    kept = box["state"] / "pycache.FULL01"
    kept.mkdir()
    (kept / "x").write_text("x")
    outside = box["tmp"] / "pycache.OUT001"
    outside.mkdir()
    assert script(box, "run-observe.sh").returncode == 0
    assert not stale.exists()
    # Never emptied by force, and never anything outside the state directory.
    assert (kept / "x").exists()
    assert outside.exists()


# ------------------------------------------------------------ stop and restart


def test_the_stop_file_skips_every_run(box):
    box["state"].mkdir()
    (box["state"] / "STOP").touch()
    result = script(box, "run-observe.sh")
    assert result.returncode == 0
    assert "skipped STOP_FILE_PRESENT" in log(box)
    assert not (box["record"] / "args").exists()


def detached_sleep(seconds: int, name: str = "run-observe.sh") -> int:
    """A process that is not ours (reaped by init, never a zombie), named like a wrapper."""
    started = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"(exec -a {name} /bin/sleep {seconds}) >/dev/null 2>&1 & echo $!",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(started.stdout.strip())


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def started(pid: int) -> str:
    return subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    ).stdout.strip("\n")


def hold_lock(box: dict[str, Path], pid: int, since: str | None = None) -> None:
    """A lock as the wrapper writes it: its pid and that process's start time."""
    (box["state"] / "run.lock").mkdir(parents=True)
    since = started(pid) if since is None else since
    (box["state"] / "run.lock" / "pid").write_text(f"{pid}\n{since}\n")


def test_a_run_in_progress_is_never_joined_by_a_second(box):
    pid = detached_sleep(30)
    try:
        hold_lock(box, pid)
        result = script(box, "run-observe.sh")
        assert result.returncode == 0
        assert f"skipped RUN_IN_PROGRESS pid {pid}" in log(box)
        assert not (box["record"] / "args").exists()
        assert (box["state"] / "run.lock" / "pid").read_text().split("\n")[0] == str(pid)
    finally:
        os.kill(pid, signal.SIGKILL)


def test_a_lock_without_a_pid_counts_as_held(box):
    (box["state"] / "run.lock").mkdir(parents=True)
    assert script(box, "run-observe.sh").returncode == 0
    assert "skipped RUN_IN_PROGRESS" in log(box)
    assert not (box["record"] / "args").exists()


@pytest.mark.parametrize("pid", [999999, os.getpid()])
def test_a_dead_or_foreign_pid_does_not_block_the_next_run(box, pid):
    hold_lock(box, pid)
    assert script(box, "run-observe.sh").returncode == 0
    assert (box["record"] / "args").exists()
    assert not (box["state"] / "run.lock").exists()


def test_a_terminated_wrapper_stops_the_scout_and_waits_for_it(box):
    (box["record"] / "sleep").touch()
    wrapper = subprocess.Popen(
        ["/bin/bash", str(box["checkout"] / "ops" / "observe" / "run-observe.sh")],
        env=environment(box),
    )
    deadline = time.monotonic() + 10
    while not (box["record"] / "started").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    wrapper.send_signal(signal.SIGTERM)
    assert wrapper.wait(timeout=20) == 143
    assert (box["record"] / "signal").read_text().strip() == "terminated"
    assert "observe: exit 143" in log(box)
    assert not (box["state"] / "run.lock").exists()


def test_stop_sets_the_stop_file_and_unloads(box):
    result = script(box, "stop.sh")
    assert result.returncode == 0, result.stderr
    assert (box["state"] / "STOP").exists()
    assert "bootout" in (box["record"] / "launchctl").read_text()


def test_stop_waits_for_the_run_in_progress(box):
    pid = detached_sleep(2)
    hold_lock(box, pid)
    started = time.monotonic()
    result = script(box, "stop.sh")
    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started >= 1.5
    assert not alive(pid)
    assert "bootout" in (box["record"] / "launchctl").read_text()


def test_stop_gives_up_loudly_and_keeps_the_stop_file(box):
    pid = detached_sleep(30)
    try:
        hold_lock(box, pid)
        result = script(box, "stop.sh", RH_AGENTS_OBSERVE_STOP_WAIT_SECONDS="1")
        assert result.returncode == 1
        assert "OBSERVE_STOP_TIMEOUT" in result.stderr
        assert (box["state"] / "STOP").exists()
        assert not (box["record"] / "launchctl").exists()
    finally:
        os.kill(pid, signal.SIGKILL)


def test_stop_now_interrupts_the_run_in_progress(box):
    pid = detached_sleep(30)
    hold_lock(box, pid)
    result = script(box, "stop.sh", "--now")
    assert result.returncode == 0, result.stderr
    assert not alive(pid)


def test_stop_never_signals_a_foreign_process_behind_a_stale_pid(box):
    pid = detached_sleep(30, name="unrelated")
    try:
        hold_lock(box, pid)
        assert script(box, "stop.sh", "--now").returncode == 0
        assert alive(pid)
    finally:
        os.kill(pid, signal.SIGKILL)


def test_stop_fails_loudly_while_the_agent_stays_loaded(box):
    (box["record"] / "loaded").touch()
    result = script(box, "stop.sh")
    assert result.returncode == 1
    assert "OBSERVE_STOP_FAILED" in result.stderr
    assert (box["state"] / "STOP").exists()


# ------------------------------------------------------------ start


def test_start_needs_an_explicit_mode(box):
    result = script(box, "start.sh")
    assert result.returncode == 78
    assert not (box["record"] / "launchctl").exists()


def test_the_rendered_agent_runs_the_wrapper_every_five_minutes(box):
    result = script(box, "start.sh", "--dry-run")
    assert result.returncode == 0, result.stderr
    plist = plistlib.loads(result.stdout.encode())
    checkout = box["checkout"]
    assert plist["Label"] == "com.rh-agents.observe"
    assert plist["ProgramArguments"][1] == f"{checkout}/ops/observe/run-observe.sh"
    assert plist["StartInterval"] == 300
    assert plist["RunAtLoad"] is False
    assert plist["EnvironmentVariables"]["RH_AGENTS_OBSERVE_ENV"] == str(box["env_file"])
    assert not (box["record"] / "launchctl").exists()


def test_start_refuses_a_commit_that_is_not_on_main(box):
    not_on_main(box)
    result = script(box, "start.sh", "--confirm")
    assert result.returncode == 78
    assert "OBSERVE_COMMIT_NOT_ON_MAIN" in result.stderr
    assert not (box["record"] / "launchctl").exists()


def test_start_refuses_a_profile_that_could_trade(box):
    profile_with(box, PAPER_RUNNER_ENABLED="true")
    result = script(box, "start.sh", "--confirm")
    assert result.returncode == 78
    assert "OBSERVE_REQUIRES_PAPER_RUNNER_ENABLED" in result.stderr


def test_start_clears_the_stop_file_and_loads_the_agent(box):
    box["state"].mkdir()
    (box["state"] / "STOP").touch()
    executable(box["bin"] / "plutil", "exit 0\n")
    result = script(box, "start.sh", "--confirm")
    assert result.returncode == 0, result.stderr
    assert not (box["state"] / "STOP").exists()
    assert "bootstrap" in (box["record"] / "launchctl").read_text()
    assert (box["tmp"] / "Library" / "LaunchAgents" / "com.rh-agents.observe.plist").exists()


def test_a_rendered_path_with_a_backslash_is_refused(box):
    result = script(
        box,
        "start.sh",
        "--dry-run",
        RH_AGENTS_OBSERVE_LOG_DIR=str(box["tmp"] / "a\\&b"),
    )
    assert result.returncode == 78
    assert "OBSERVE_PATH_NOT_PLAIN" in result.stderr


@pytest.mark.parametrize("interval", ["60", "299", "3601", "5m"])
def test_the_interval_stays_within_five_minutes_and_an_hour(box, interval):
    result = script(
        box,
        "start.sh",
        "--dry-run",
        RH_AGENTS_OBSERVE_INTERVAL_SECONDS=interval,
    )
    assert result.returncode == 78


# ------------------------------------------------------------ status


def fake_psql(box: dict[str, Path], invariants: str) -> None:
    executable(
        box["bin"] / "psql",
        'query="${@: -1}"\n'
        'case "$query" in\n'
        "  *alembic_version*) echo 0026 ;;\n"
        "  *extract*) echo 120 ;;\n"
        '  *"order by started_at desc limit 1"*) echo COMPLETED ;;\n'
        f'  *trade_cases*) echo "{invariants}" ;;\n'
        "  *) echo 0 ;;\n"
        "esac\n",
    )


def test_status_is_healthy_without_trading_state(box):
    fake_psql(box, "0 0 0")
    box["state"].mkdir()
    (box["state"] / "STOP").touch()
    result = script(box, "status.sh")
    assert result.returncode == 0, result.stdout
    assert "trade_cases=0 order_intents=0 positions=0" in result.stdout
    assert "HEALTHY" in result.stdout


def test_status_reports_a_lock_without_a_live_run(box):
    fake_psql(box, "0 0 0")
    (box["state"] / "run.lock").mkdir(parents=True)
    (box["state"] / "STOP").touch()
    result = script(box, "status.sh")
    assert result.returncode == 1
    assert "STALE_RUN_LOCK" in result.stdout


def test_status_fails_closed_when_the_database_cannot_be_read(box):
    executable(box["bin"] / "psql", "exit 2\n")
    result = script(box, "status.sh")
    assert result.returncode == 2
    assert "trading invariants: UNKNOWN" in result.stdout


def test_status_flags_any_trading_state_in_the_observation_database(box):
    fake_psql(box, "0 0 1")
    result = script(box, "status.sh")
    assert result.returncode == 2
    assert "VIOLATED" in result.stdout


# ------------------------------------------------------------ the files themselves


def test_nothing_but_the_scout_mode_is_ever_started():
    for name in FILES:
        text = (OBSERVE / name).read_text()
        for mode in ("--once", "--entries-once", "--exits-once"):
            assert mode not in text.replace("--scout-once", ""), (name, mode)


def test_no_secret_is_committed():
    for name in FILES:
        text = (OBSERVE / name).read_text().lower()
        for marker in ("sk-ant", "api_key=", "apikey", "password=", "secret="):
            assert marker not in text, (name, marker)


# ------------------------------------------------------------ inherited shell state


def dirty_checkout(box: dict[str, Path]) -> None:
    (box["checkout"] / "ops" / "observe" / "common.sh").write_text(
        (box["checkout"] / "ops" / "observe" / "common.sh").read_text() + "\n# modified\n"
    )


def test_a_function_from_bash_env_never_reaches_the_checks(box):
    hook = box["tmp"] / "hook.sh"
    hook.write_text(f'env() {{ touch "{box["record"]}/hijacked"; command env "$@"; }}\n')
    result = script(box, "run-observe.sh", BASH_ENV=str(hook))
    assert result.returncode == 0, result.stderr
    assert (box["record"] / "args").exists()
    assert not (box["record"] / "hijacked").exists()


def test_an_exported_function_never_replaces_a_command(box):
    dirty_checkout(box)
    result = script(box, "run-observe.sh", **{"BASH_FUNC_git%%": "() { return 0; }"})
    assert result.returncode == 78
    assert "OBSERVE_WORKTREE_MODIFIED" in result.stderr
    assert not (box["record"] / "args").exists()


def test_an_inherited_git_dir_never_vouches_for_a_modified_checkout(box):
    clean = box["tmp"] / "clean"
    shutil.copytree(box["checkout"], clean)
    dirty_checkout(box)
    result = script(box, "run-observe.sh", GIT_DIR=str(clean / ".git"), GIT_WORK_TREE=str(clean))
    assert result.returncode == 78
    assert "OBSERVE_WORKTREE_MODIFIED" in result.stderr
    assert not (box["record"] / "args").exists()


@pytest.mark.parametrize("record", ["{pid}\n", "{pid}\nThu Jan  1 00:00:00 1970\n"])
def test_a_reused_pid_is_never_trusted_by_its_name_alone(box, record):
    """A wrapper-named process behind a pid without its own start time is foreign."""
    pid = detached_sleep(30)
    try:
        (box["state"] / "run.lock").mkdir(parents=True)
        (box["state"] / "run.lock" / "pid").write_text(record.format(pid=pid))
        assert script(box, "stop.sh", "--now").returncode == 0
        assert alive(pid)
        (box["state"] / "STOP").unlink()
        assert script(box, "run-observe.sh").returncode == 0
        assert (box["record"] / "args").exists()
        assert alive(pid)
    finally:
        if alive(pid):
            os.kill(pid, signal.SIGKILL)


def test_the_lock_names_the_wrapper_and_its_start_time(box):
    (box["record"] / "sleep").touch()
    wrapper = subprocess.Popen(
        ["/bin/bash", str(box["checkout"] / "ops" / "observe" / "run-observe.sh")],
        env=environment(box),
    )
    try:
        deadline = time.monotonic() + 10
        while not (box["record"] / "started").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        pid, since = (box["state"] / "run.lock" / "pid").read_text().split("\n")[:2]
        assert int(pid) == wrapper.pid
        assert since == started(wrapper.pid)
        assert script(box, "status.sh").stdout.count(f"run in progress: pid {pid}") == 1
    finally:
        wrapper.send_signal(signal.SIGTERM)
        wrapper.wait(timeout=20)


def test_an_inherited_path_never_chooses_git(box):
    executable(box["bin"] / "git", "exit 0\n")
    dirty_checkout(box)
    result = script(box, "run-observe.sh")
    assert result.returncode == 78
    assert "OBSERVE_WORKTREE_MODIFIED" in result.stderr
    assert not (box["record"] / "args").exists()


def test_an_inherited_interpreter_never_runs(box):
    shim = executable(box["tmp"] / "shim", f'touch "{box["record"]}/shim"\n')
    result = script(box, "run-observe.sh", RH_AGENTS_OBSERVE_PYTHON=str(shim))
    assert result.returncode == 0, result.stderr
    assert (box["record"] / "args").exists()
    assert not (box["record"] / "shim").exists()


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_a_hidden_edit_still_counts_as_modified(box, flag):
    dirty_checkout(box)
    subprocess.run(
        ["git", "-C", str(box["checkout"]), "update-index", flag, "ops/observe/common.sh"],
        check=True,
    )
    result = script(box, "run-observe.sh")
    assert result.returncode == 78
    assert "OBSERVE_WORKTREE_MODIFIED" in result.stderr


# ------------------------------------------------------------ test hooks stay in tests


def shim(box: dict[str, Path]) -> Path:
    return executable(box["tmp"] / "shim", f'touch "{box["record"]}/shim"\n')


def not_on_main(box: dict[str, Path]) -> None:
    subprocess.run(
        ["git", "-C", str(box["checkout"]), "update-ref", "-d", "refs/remotes/origin/main"],
        capture_output=True,
    )


@pytest.mark.parametrize(
    ("name", "hook", "args"),
    [
        ("stop.sh", "RH_AGENTS_LAUNCHCTL", ()),
        ("status.sh", "RH_AGENTS_LAUNCHCTL", ()),
        ("status.sh", "RH_AGENTS_PSQL", ()),
        ("start.sh", "RH_AGENTS_PLUTIL", ("--confirm",)),
        ("start.sh", "RH_AGENTS_LAUNCHCTL", ("--confirm",)),
    ],
)
def test_an_inherited_test_hook_never_runs_a_command(box, name, hook, args):
    executable(box["bin"] / "plutil", "exit 0\n")
    script(box, name, *args, **{hook: str(shim(box)), "RH_AGENTS_OBSERVE_ALLOW_UNMERGED": "1"})
    assert not (box["record"] / "shim").exists()


def test_an_inherited_agents_directory_is_ignored(box):
    executable(box["bin"] / "plutil", "exit 0\n")
    elsewhere = box["tmp"] / "elsewhere"
    script(
        box,
        "start.sh",
        "--confirm",
        RH_AGENTS_LAUNCH_AGENTS=str(elsewhere),
        RH_AGENTS_OBSERVE_ALLOW_UNMERGED="1",
    )
    assert not (elsewhere / "com.rh-agents.observe.plist").exists()


def test_an_inherited_allow_unmerged_never_skips_the_main_check(box):
    not_on_main(box)
    result = script(box, "start.sh", "--confirm", RH_AGENTS_OBSERVE_ALLOW_UNMERGED="1")
    assert result.returncode == 78
    assert "OBSERVE_COMMIT_NOT_ON_MAIN" in result.stderr
    assert not (box["record"] / "launchctl").exists()


def test_a_run_refuses_a_commit_that_is_not_on_main(box):
    not_on_main(box)
    result = script(box, "run-observe.sh")
    assert result.returncode == 78
    assert "OBSERVE_COMMIT_NOT_ON_MAIN" in result.stderr
    assert not (box["record"] / "args").exists()


@pytest.mark.parametrize("key", ["CODEX_EXECUTABLE", "RH_AGENTS_OBSERVE_PSQL"])
def test_a_command_in_the_profile_must_be_absolute(box, key):
    profile_with(box, **{key: "./bin/tool"})
    result = script(box, "run-observe.sh", "--check")
    assert result.returncode == 78
    assert f"OBSERVE_{key}_NOT_ABSOLUTE" in result.stderr
