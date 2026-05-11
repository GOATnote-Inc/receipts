"""External-system connectors (C team).

Each connector is a thin, hermetic wrapper around a single SaaS API. Real
network calls live behind a caller-supplied client so tests can inject
``MagicMock`` and CI never depends on third-party uptime.

The reconciler (P1-6) consumes the typed pydantic models exported here;
connectors do not import from ``judge`` or ``ledger`` to keep import-time
side effects nil.
"""

from __future__ import annotations

from receipts.connectors.granola import (
    GranolaConnector,
    GranolaDecision,
    GranolaMeeting,
)

__all__ = [
    "GranolaConnector",
    "GranolaDecision",
    "GranolaMeeting",
]
