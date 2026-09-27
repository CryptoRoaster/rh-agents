"""Evaluation harnesses, and the gated Codex path the runtime may opt into.

Nothing under this package may be imported by `src.agents`, `src.orchestration`,
`src.runner`, `src.risk`, `src.execution` or `src.ledger`, and nothing here
reads `Settings`. The only bridge is `src.codex_reasoning`, which the runtime
imports lazily when `REASONING_PROVIDER=codex` is chosen. Tests enforce all of
it, so the separation is a property of the build rather than a promise in a
document.
"""
