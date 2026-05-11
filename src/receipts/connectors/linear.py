"""Linear connector (P1-1) — GraphQL adapter over the Linear API.

The connector is a thin shim around an injectable ``httpx.Client``. The Linear
GraphQL endpoint is the only network surface; every call goes through
``client.post(base_url, json=..., headers=...)`` so test suites can pin
responses with a ``MagicMock`` and avoid the network entirely.

Acceptance-criteria parsing
---------------------------
Epics in Linear store their acceptance criteria inside the free-form
``description`` field. The drafter (P1-5) and the CEIS judge stack need a
structured list, so this module extracts both numbered lists
(``1.`` / ``1)``) AND bulleted lists (``-`` / ``*``) from the description,
stripping markdown emphasis (``**bold**``, ``_italic_``) along the way.

What this module deliberately does NOT do
-----------------------------------------
- No retry / backoff: the reconciler (P1-6) is responsible for replay.
- No persistence: writing into the ledger happens in P1-6, not here.
- No streaming pagination: Linear's ``projects(first: N)`` is sufficient
  for the weekly-cycle fixture; larger sweeps can iterate page cursors in
  a follow-up.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field

# ----------------------------- GraphQL queries -----------------------------

_EPIC_FIELDS = """
  id
  title
  description
  state { name }
  createdAt
  updatedAt
"""

_QUERY_FETCH_EPICS = (
    """
query FetchEpics($teamId: String!, $since: DateTime) {
  team(id: $teamId) {
    projects(filter: { updatedAt: { gt: $since } }) {
      nodes {
"""
    + _EPIC_FIELDS
    + """
      }
    }
  }
}
""".strip()
)

_QUERY_FETCH_EPIC_BY_ID = (
    """
query FetchEpicById($id: String!) {
  project(id: $id) {
"""
    + _EPIC_FIELDS
    + """
  }
}
""".strip()
)

_MUTATION_ADD_COMMENT = """
mutation AddComment($projectId: String!, $body: String!) {
  commentCreate(input: { projectId: $projectId, body: $body }) {
    success
    comment { id }
  }
}
""".strip()


# ----------------------------- description parsing -----------------------------

_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")

# Match `**bold**` / `__bold__` (paired delimiter) -- always safe to strip.
_STRONG_RE = re.compile(r"(\*\*|__)(.+?)\1")
# Match single `*italic*` / `_italic_` only when the delimiter is NOT flanked
# by word characters on both sides; this prevents eating `_` inside identifiers
# like ``fetch_epic_by_id``.
_EMPH_RE = re.compile(r"(?<!\w)([*_])(?!\s)(.+?)(?<!\s)\1(?!\w)")


def _strip_markdown(text: str) -> str:
    """Strip ``**bold**`` / ``__bold__`` / ``*italic*`` / ``_italic_``.

    Single-character delimiters are only stripped when they don't sit inside
    a word so identifiers like ``fetch_epic_by_id`` survive intact.
    """
    prev = None
    out = text
    # Repeat until stable so nested emphasis collapses cleanly.
    while prev != out:
        prev = out
        out = _STRONG_RE.sub(r"\2", out)
        out = _EMPH_RE.sub(r"\2", out)
    return out.strip()


def parse_acceptance_criteria(description: str) -> list[str]:
    """Extract numbered + bulleted list items from a markdown description.

    Numbered items (``1.`` / ``1)``) and bulleted items (``-`` / ``*``)
    are both recognised. Items appear in the order they're found in the
    text; markdown emphasis inside each item is stripped.
    """
    if not description:
        return []
    items: list[str] = []
    for raw_line in description.splitlines():
        m = _NUMBERED_RE.match(raw_line) or _BULLET_RE.match(raw_line)
        if m:
            items.append(_strip_markdown(m.group(1)))
    return items


# ----------------------------- models -----------------------------


class LinearEpic(BaseModel):
    """A Linear project (called an "epic" in receipts parlance).

    ``acceptance_criteria_parsed`` is derived from ``description`` at parse
    time so downstream callers don't re-implement the regex.
    """

    external_id: str
    title: str
    description: str
    state: str
    created_at: datetime
    updated_at: datetime
    acceptance_criteria_parsed: list[str] = Field(default_factory=list)


# ----------------------------- connector -----------------------------


class LinearConnector:
    """Thin GraphQL client for the Linear API.

    Parameters
    ----------
    api_key:
        Personal API key or OAuth token; sent as ``Authorization: Bearer ...``.
    client:
        Optional ``httpx.Client``. Inject a ``MagicMock`` in tests; pass
        ``None`` in production and the connector owns a new client per call.
    base_url:
        GraphQL endpoint. Override only for self-hosted / staging.
    """

    def __init__(
        self,
        api_key: str,
        client: httpx.Client | None = None,
        base_url: str = "https://api.linear.app/graphql",
    ) -> None:
        self._api_key = api_key
        self._client = client
        self._base_url = base_url

    # ----- internals -----

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """POST a GraphQL request and return the decoded JSON body.

        Uses the injected client when supplied; otherwise spins up a
        short-lived ``httpx.Client`` for this single call.
        """
        body = {"query": query, "variables": variables}
        if self._client is not None:
            response = self._client.post(self._base_url, json=body, headers=self._headers())
        else:
            with httpx.Client() as client:
                response = client.post(self._base_url, json=body, headers=self._headers())
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _parse_epic_node(node: dict[str, Any]) -> LinearEpic:
        state = node.get("state") or {}
        state_name = state.get("name", "") if isinstance(state, dict) else ""
        description = node.get("description", "") or ""
        return LinearEpic(
            external_id=node["id"],
            title=node.get("title", "") or "",
            description=description,
            state=state_name,
            created_at=node["createdAt"],
            updated_at=node["updatedAt"],
            acceptance_criteria_parsed=parse_acceptance_criteria(description),
        )

    # ----- public API -----

    def fetch_epics(
        self,
        team_id: str,
        since: datetime | None = None,
    ) -> list[LinearEpic]:
        """List projects (epics) for ``team_id`` updated after ``since``."""
        variables: dict[str, Any] = {"teamId": team_id}
        if since is not None:
            variables["since"] = since.isoformat()
        payload = self._post(_QUERY_FETCH_EPICS, variables)
        data = payload.get("data") or {}
        team = data.get("team") or {}
        projects = team.get("projects") or {}
        nodes = projects.get("nodes") or []
        return [self._parse_epic_node(n) for n in nodes]

    def fetch_epic_by_id(self, external_id: str) -> LinearEpic | None:
        """Look up a single project. Returns ``None`` on NOT_FOUND."""
        payload = self._post(_QUERY_FETCH_EPIC_BY_ID, {"id": external_id})
        # Linear returns errors[] + data.project=null when the id is unknown.
        if payload.get("errors"):
            return None
        data = payload.get("data") or {}
        node = data.get("project")
        if node is None:
            return None
        return self._parse_epic_node(node)

    def add_comment(self, epic_external_id: str, body: str) -> str:
        """Post a comment onto a project and return the new comment id."""
        variables = {"projectId": epic_external_id, "body": body}
        payload = self._post(_MUTATION_ADD_COMMENT, variables)
        data = payload.get("data") or {}
        result = data.get("commentCreate") or {}
        comment = result.get("comment") or {}
        comment_id = comment.get("id")
        if not comment_id:
            raise RuntimeError(f"Linear commentCreate returned no comment id: {payload!r}")
        return str(comment_id)
