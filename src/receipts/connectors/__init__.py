"""External-service connectors for the Engineering Receipts vertical.

Each connector is a thin, dependency-injected adapter over a vendor API
(REST, GraphQL, or MCP shim). Connectors return pydantic v2 models pinned
to the wire shape the rest of the receipts pipeline expects. Connectors
must never read `.env` or hold global state; call sites pass credentials
and an httpx.Client explicitly so tests can swap in MagicMock instances
and `make test` stays hermetic.
"""

from __future__ import annotations

from receipts.connectors.fhir import (
    ATTESTATION_EXTENSION_URL,
    AttestationExtension,
    FhirComposition,
    FHIRConnector,
    FhirReference,
    FhirSection,
)
from receipts.connectors.github import GitHubCommit, GitHubConnector, GitHubPR
from receipts.connectors.granola import (
    GranolaConnector,
    GranolaDecision,
    GranolaMeeting,
)
from receipts.connectors.linear import LinearConnector, LinearEpic
from receipts.connectors.slack import SlackConnector, SlackThread

__all__ = [
    "ATTESTATION_EXTENSION_URL",
    "AttestationExtension",
    "FHIRConnector",
    "FhirComposition",
    "FhirReference",
    "FhirSection",
    "GitHubCommit",
    "GitHubConnector",
    "GitHubPR",
    "GranolaConnector",
    "GranolaDecision",
    "GranolaMeeting",
    "LinearConnector",
    "LinearEpic",
    "SlackConnector",
    "SlackThread",
]
