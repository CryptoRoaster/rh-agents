"""Offline evaluation harnesses. Never part of the production runtime.

Nothing under this package may be imported by `src.agents`, `src.orchestration`,
`src.runner`, `src.risk`, `src.execution` or `src.ledger`, and nothing here is
reachable from `Settings` or `ports_from_settings`. A test enforces both, so the
separation is a property of the build rather than a promise in a document.
"""
