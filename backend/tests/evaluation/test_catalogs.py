"""The committed gpt-5.5 catalog: unmodified, accepted, and pinned by digest."""

import hashlib
import json
import shutil
import subprocess

import pytest

from src.evaluation.codex.catalog import judge_catalog
from src.evaluation.codex.catalogs import (
    GPT_5_5_CATALOG,
    GPT_5_5_CATALOG_SHA256,
    canonical_bytes,
    single_model_catalog,
)
from src.evaluation.codex.models import SUPPORTED_CLI_VERSION

CODEX = shutil.which("codex")


def bundled_catalog() -> str | None:
    """The catalog compiled into the installed build, or None if absent.

    `--bundled` makes no request of any kind; it prints what the binary ships.
    """
    if CODEX is None:
        return None
    try:
        version = subprocess.run(
            [CODEX, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
        if SUPPORTED_CLI_VERSION not in (version.stdout + version.stderr):
            return None
        dump = subprocess.run(
            [CODEX, "debug", "models", "--bundled"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment
        return None
    return dump.stdout if dump.returncode == 0 else None


BUNDLED = bundled_catalog()


def test_the_committed_catalog_is_pinned_by_its_own_bytes() -> None:
    raw = GPT_5_5_CATALOG.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == GPT_5_5_CATALOG_SHA256


def test_any_changed_byte_breaks_the_pin() -> None:
    raw = bytearray(GPT_5_5_CATALOG.read_bytes())
    raw[-1] = raw[-1] ^ 0x01
    assert hashlib.sha256(bytes(raw)).hexdigest() != GPT_5_5_CATALOG_SHA256


def test_the_committed_catalog_is_accepted_for_gpt_5_5() -> None:
    verdict = judge_catalog(GPT_5_5_CATALOG.read_text(encoding="utf-8"), "gpt-5.5")
    assert verdict.reason is None
    assert verdict.surface is not None
    assert verdict.surface.tool_mode is None
    assert verdict.surface.use_responses_lite is False
    assert verdict.surface.experimental_supported_tools == ()


def test_it_holds_exactly_one_entry() -> None:
    payload = json.loads(GPT_5_5_CATALOG.read_text(encoding="utf-8"))
    assert list(payload) == ["models"]
    assert len(payload["models"]) == 1
    assert payload["models"][0]["slug"] == "gpt-5.5"


def test_the_serialisation_is_the_canonical_one() -> None:
    """Formatting is part of the contract, because the digest covers the bytes."""
    payload = json.loads(GPT_5_5_CATALOG.read_text(encoding="utf-8"))
    assert canonical_bytes(payload) == GPT_5_5_CATALOG.read_bytes()


@pytest.mark.skipif(BUNDLED is None, reason=f"Codex {SUPPORTED_CLI_VERSION} is not installed")
def test_no_field_was_modified_on_the_way_in() -> None:
    """A filter, not an edit. Every field is the vendor's own."""
    assert BUNDLED is not None
    source = json.loads(BUNDLED)
    original = next(m for m in source["models"] if m["slug"] == "gpt-5.5")
    committed = json.loads(GPT_5_5_CATALOG.read_text(encoding="utf-8"))["models"][0]
    assert committed == original
    differing = {
        key for key in set(committed) | set(original) if committed.get(key) != original.get(key)
    }
    assert differing == set()


@pytest.mark.skipif(BUNDLED is None, reason=f"Codex {SUPPORTED_CLI_VERSION} is not installed")
def test_the_generator_reproduces_the_committed_bytes() -> None:
    assert BUNDLED is not None
    assert single_model_catalog(BUNDLED, "gpt-5.5") == GPT_5_5_CATALOG.read_bytes()
