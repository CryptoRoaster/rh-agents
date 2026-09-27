"""The harness stays outside the runtime except through one explicit bridge.

Codex became selectable as `REASONING_PROVIDER=codex`. That has to stay more
than a sentence in a docstring, so these tests make it a property of the tree:
no runtime package imports the harness, the only bridge is
`src.codex_reasoning`, it is reached lazily and only when chosen, and the
default configuration selects nothing.
"""

import ast
from pathlib import Path

import pytest

from src.core.config import Settings

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"

RUNTIME_PACKAGES = (
    "agents",
    "api",
    "core",
    "data",
    "execution",
    "ledger",
    "markets",
    "orchestration",
    "reasoning",
    "risk",
    "runner",
    "runtime",
)


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
    return names


@pytest.mark.parametrize("package", RUNTIME_PACKAGES)
def test_no_runtime_package_imports_the_probe(package: str) -> None:
    root = SOURCE_ROOT / package
    if not root.exists():  # pragma: no cover - guards a future rename
        pytest.skip(f"src/{package} does not exist")
    offenders = [
        str(path.relative_to(SOURCE_ROOT))
        for path in root.rglob("*.py")
        if any(name.startswith("src.evaluation") for name in imported_modules(path))
    ]
    assert offenders == []


def test_codex_is_selectable_only_explicitly() -> None:
    field = Settings.model_fields["reasoning_provider"]
    allowed = str(field.annotation)
    assert "codex" in allowed
    assert field.default == "disabled"


def test_only_the_bridge_imports_the_harness() -> None:
    offenders = [
        str(path.relative_to(SOURCE_ROOT))
        for path in SOURCE_ROOT.rglob("*.py")
        if not path.is_relative_to(SOURCE_ROOT / "evaluation")
        and not path.is_relative_to(SOURCE_ROOT / "codex_reasoning")
        and any(name.startswith("src.evaluation") for name in imported_modules(path))
    ]
    assert offenders == []


def module_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


@pytest.mark.parametrize("module", ["runner/composition.py", "scout/service.py"])
def test_the_bridge_is_imported_only_when_codex_is_chosen(module: str) -> None:
    path = SOURCE_ROOT / module
    assert not any(name.startswith("src.codex_reasoning") for name in module_level_imports(path))
    assert "src.codex_reasoning.provider" in imported_modules(path)
    assert "src.evaluation" not in path.read_text(encoding="utf-8")


def test_the_reasoning_contract_is_untouched_by_the_probe() -> None:
    for module in (SOURCE_ROOT / "reasoning").rglob("*.py"):
        text = module.read_text(encoding="utf-8")
        assert "codex" not in text.lower()
    models = (SOURCE_ROOT / "reasoning" / "models.py").read_text(encoding="utf-8")
    # The production port still promises a generation limit; the probe simply
    # does not offer one rather than weakening this.
    assert "max_output_tokens: int = Field(ge=64, le=8192)" in models


def test_the_probe_reads_nothing_from_settings() -> None:
    # Checked through the import graph, not through text: a docstring may name
    # Settings while explaining why the probe never reads it.
    for module in (SOURCE_ROOT / "evaluation").rglob("*.py"):
        assert "src.core.config" not in imported_modules(module)
        assert "Settings" not in imported_names(module)


def test_the_probe_imports_no_orbit_production_code() -> None:
    for module in (SOURCE_ROOT / "evaluation").rglob("*.py"):
        assert not any(name.startswith("src.agents") for name in imported_modules(module))
