"""Vendor MCP shims (C team).

Each connector is a thin httpx wrapper over a vendor's Web API. Connectors
return pydantic v2 models so downstream consumers (drafter, reconciler) can
treat Linear / GitHub / Slack / Granola payloads uniformly.

Live API calls are gated behind injected ``httpx.Client`` instances so that
tests can swap in MagicMock without ever touching the network — see
``tests/test_connector_slack.py``.
"""

from __future__ import annotations

from receipts.connectors.slack import SlackConnector, SlackThread

__all__ = [
    "SlackConnector",
    "SlackThread",
]
