"""The probe stays outside the runtime, and the runtime stays unaware of it.

"Test artifact, not workflow evidence" has to be more than a sentence in a
docstring. These tests make it a property of the tree: no runtime package can
import the harness, and no configuration path can select it.
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


def test_the_probe_is_not_a_selectable_reasoning_provider() -> None:
    field = Settings.model_fields["reasoning_provider"]
    allowed = str(field.annotation)
    assert "codex" not in allowed
    assert "anthropic" in allowed


def test_production_composition_mentions_no_codex_wiring() -> None:
    composition = (SOURCE_ROOT / "runner" / "composition.py").read_text(encoding="utf-8")
    assert "codex" not in composition.lower()
    assert "evaluation" not in composition.lower()


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
