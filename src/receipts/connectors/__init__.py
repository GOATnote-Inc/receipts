"""External-service connectors (C team).

Each connector is a thin, dependency-injected client wrapping a vendor REST API
(or MCP shim). Connectors return pydantic v2 models pinned to the wire shape
the rest of the receipts pipeline expects — see e.g. ``drafter.PRRef`` for the
downstream-facing analog. Connectors must never read ``.env`` or hold global
state; every call site passes credentials and an ``httpx.Client`` explicitly so
tests can swap in mocks.
"""

from __future__ import annotations

from receipts.connectors.github import GitHubCommit, GitHubConnector, GitHubPR

__all__ = [
    "GitHubCommit",
    "GitHubConnector",
    "GitHubPR",
]
