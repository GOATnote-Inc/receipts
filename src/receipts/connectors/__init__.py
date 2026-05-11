"""Vendor connector shims for the Engineering Receipts vertical.

Each connector here is a thin, dependency-injected adapter over a vendor
API. Connectors NEVER make a real network call from the test suite -- every
test injects a ``MagicMock`` client so ``make test`` stays hermetic.
"""

from receipts.connectors.linear import LinearConnector, LinearEpic

__all__ = ["LinearConnector", "LinearEpic"]
