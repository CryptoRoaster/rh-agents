"""Without the supervisor, nothing changes: no setting, no mode, no import."""

import ast
import subprocess
import sys
from pathlib import Path

from src.core.config import Settings

SRC = Path(__file__).resolve().parents[2] / "src"


def test_no_runtime_module_depends_on_the_supervisor():
    for path in SRC.rglob("*.py"):
        if path.is_relative_to(SRC / "supervisor"):
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("src.supervisor"), path
            if isinstance(node, ast.Import):
                assert not any(item.name.startswith("src.supervisor") for item in node.names), path


def test_the_runner_and_the_api_load_without_it():
    code = (
        "import sys, src.runner.main, src.api.main\n"
        "assert not any(name.startswith('src.supervisor') for name in sys.modules)\n"
    )
    subprocess.run(
        [sys.executable, "-c", code],
        cwd=SRC.parent,
        check=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", "DATABASE_URL": "postgresql+asyncpg://x@localhost/x"},
    )


def test_the_exit_settings_keep_their_defaults():
    fields = Settings.model_fields
    assert fields["paper_runner_enabled"].default is False
    assert fields["paper_entry_requires_exit_job"].default is False
    assert fields["early_paper_exit_enabled"].default is False
    assert fields["paper_auto_exit_enabled"].default is False
    assert fields["paper_exit_run_max_seconds"].default == 60
    assert fields["paper_exit_liveness_max_age_seconds"].default == 180
