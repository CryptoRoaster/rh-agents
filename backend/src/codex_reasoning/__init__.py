"""The one bridge from the structured reasoning port to the Codex harness.

`src.evaluation.codex` stays free of Settings and of every runtime package.
This package is the only place outside it that imports the harness, and the
runtime reaches it only lazily, when `REASONING_PROVIDER=codex` was chosen.
"""
