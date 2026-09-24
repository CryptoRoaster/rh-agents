"""Reviewed single-model catalogs, and the deterministic way they are built.

A pinned catalog is only reviewable if the same input always produces the same
bytes: the digest covers the file exactly as committed, so formatting is part
of the contract. `canonical_bytes` fixes that form -- sorted keys, two-space
indent, no ASCII escaping, one trailing newline -- and the generator applies
nothing else.

The transformation is a filter and nothing more. One entry is selected from the
catalog the supported build ships and re-serialised; no field is added, removed
or rewritten. "Make the model fit" is exactly what this must not do, because the
point of pinning the whole `ModelInfo` snapshot is that the reviewer sees what
the vendor shipped.
"""

import json
from pathlib import Path
from typing import Any

CATALOG_DIR = Path(__file__).parent

# The reviewed catalog for the first real run, filtered from Codex CLI 0.153.4.
GPT_5_5_CATALOG = CATALOG_DIR / "gpt-5.5-codex-0.153.4.json"
# sha256 of that file's bytes exactly as committed.
GPT_5_5_CATALOG_SHA256 = "5997e42da1de33fff1746eca8a98c9ac26f7ed1f4a82f15a1876027682d22e77"
# The `slug` of its one entry, which is what `--model` has to be given. The file
# name carries the Codex product name; the slug is what the catalog says.
GPT_5_5_CATALOG_SLUG = "gpt-5.5"


class CatalogBuildError(Exception):
    """The requested model is not in the source catalog."""


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """The one serialisation a pinned catalog may have."""
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def single_model_catalog(bundled: str, slug: str) -> bytes:
    """Filter a bundled catalog down to one entry, changing nothing else."""
    source = json.loads(bundled)
    entries = source.get("models")
    if not isinstance(entries, list):
        raise CatalogBuildError("source catalog has no models list")
    for entry in entries:
        if isinstance(entry, dict) and entry.get("slug") == slug:
            return canonical_bytes({"models": [entry]})
    raise CatalogBuildError(f"model {slug!r} is not in the source catalog")
