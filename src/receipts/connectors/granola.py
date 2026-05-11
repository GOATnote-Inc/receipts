"""Granola connector — meetings + extracted decisions over a REST API.

Granola (https://granola.ai) records meetings and post-hoc extracts structured
decisions and epic mentions from the transcript. As of May 2026 its public
REST surface is not fully documented, so this connector is built against a
reasonable inferred shape:

  GET {base_url}/api/v1/meetings[?since=<iso8601>]
      → {"meetings": [GranolaMeeting, ...]}

  GET {base_url}/api/v1/meetings/{meeting_id}/decisions
      → {"decisions": [GranolaDecision, ...]}

Auth is a Bearer token in the ``Authorization`` header. ``base_url`` is
configurable so on-prem or staging deployments swap it without code changes.

Tests inject a ``MagicMock`` httpx client; the connector never opens a real
socket in CI. The reconciler (P1-6) consumes the parsed models verbatim.
"""

from __future__ import annotations

from datetime import datetime

import httpx
from pydantic import BaseModel, ConfigDict


class GranolaMeeting(BaseModel):
    """A meeting as recorded by Granola.

    ``transcript_ref`` is opaque to the connector — typically a Granola URL
    or an internal pointer string. ``duration_min`` is the wall-clock length
    in minutes (Granola rounds; we surface it unchanged).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    external_id: str
    title: str
    started_at: datetime
    transcript_ref: str
    attendees: list[str]
    duration_min: int


class GranolaDecision(BaseModel):
    """A decision Granola extracted from a meeting transcript.

    ``tagged_epic_external_ids`` is whatever Granola's NLP picked out as an
    epic mention (e.g. "EPIC-7" tokens). It may be empty; the reconciler
    is responsible for matching these against the Linear epic corpus.

    ``confidence`` is Granola's self-reported extraction confidence in
    ``[0.0, 1.0]``. Downstream the judge can use it as a prior.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    meeting_id: str
    decision_text: str
    tagged_epic_external_ids: list[str]
    confidence: float


class GranolaConnector:
    """Thin REST wrapper around the (inferred) Granola public API.

    The connector is intentionally dependency-light: it takes either a caller-
    supplied ``httpx.Client`` (preferred for tests + connection pooling) or
    constructs its own. Auth is a Bearer token attached as a request header
    on every call — we do not stash it on the httpx client because callers
    may legitimately reuse one client across connectors with distinct keys.
    """

    DEFAULT_BASE_URL = "https://api.granola.ai"

    def __init__(
        self,
        api_key: str,
        client: httpx.Client | None = None,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self._api_key = api_key
        self._client = client if client is not None else httpx.Client()
        # Strip a single trailing slash so URL joins are predictable.
        self._base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def fetch_meetings(self, since: datetime | None = None) -> list[GranolaMeeting]:
        """Return meetings, optionally filtered to those started after ``since``.

        ``since`` is serialized as ISO 8601 with timezone offset preserved.
        """
        params: dict[str, str] = {}
        if since is not None:
            params["since"] = since.isoformat()

        url = f"{self._base_url}/api/v1/meetings"
        response = self._client.get(url, headers=self._auth_headers(), params=params)
        response.raise_for_status()
        body = response.json()
        return [GranolaMeeting.model_validate(item) for item in body.get("meetings", [])]

    def fetch_meeting_decisions(self, meeting_id: str) -> list[GranolaDecision]:
        """Return all extracted decisions for ``meeting_id``."""
        if not meeting_id:
            raise ValueError("meeting_id is required")
        url = f"{self._base_url}/api/v1/meetings/{meeting_id}/decisions"
        response = self._client.get(url, headers=self._auth_headers())
        response.raise_for_status()
        body = response.json()
        return [GranolaDecision.model_validate(item) for item in body.get("decisions", [])]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}
