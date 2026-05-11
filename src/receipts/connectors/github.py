"""GitHub REST v3 connector (P1-2).

A small, test-shaped slice of the GitHub API used by the Engineering Receipts
reconciler:

  - :func:`GitHubConnector.fetch_prs` — list pull requests in a repo, with
    ``Link``-header pagination and ``state`` / ``since`` filters.
  - :func:`GitHubConnector.fetch_commits_for_pr` — list commits attached to a
    given PR.
  - :func:`GitHubConnector.create_pull_request` — POST ``/repos/{repo}/pulls``
    and return the new PR's ``html_url`` so the ledger can record the write.

Every method takes an ``httpx.Client`` (defaulting to one this class owns) so
unit tests can inject a ``MagicMock`` and run without touching the network. No
secrets are read from the environment — the bearer token is always passed in
explicitly.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

#: Body length above which :attr:`GitHubPR.summary` is truncated with an
#: ellipsis. PR bodies routinely run thousands of characters; the drafter only
#: needs a token-frugal preview.
SUMMARY_MAX_LEN: int = 500

#: Marker appended when truncation actually happened. Single-character ellipsis
#: keeps the cap precise (``len(summary) <= SUMMARY_MAX_LEN``).
TRUNCATION_MARKER: str = "…"

_LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


class GitHubPR(BaseModel):
    """A pull request as the receipts pipeline sees it.

    ``external_id`` is the canonical key downstream code joins on; it is
    deliberately ``"<repo>#<number>"`` rather than the GitHub ``id`` integer so
    humans reading the ledger recognize the PR at a glance and the value stays
    stable across GitHub data migrations.

    ``summary`` is a token-frugal preview of ``body``; see
    :data:`SUMMARY_MAX_LEN` and :func:`_summarize`.
    """

    model_config = ConfigDict(extra="forbid")

    external_id: str
    repo: str
    number: int
    merged_sha: str | None
    title: str
    body: str
    summary: str
    merged_at: datetime | None
    created_at: datetime
    state: str


class GitHubCommit(BaseModel):
    """A commit attached to a PR.

    ``repo`` is carried alongside ``sha`` so callers don't need to re-thread
    the repo slug when commits are passed around.
    """

    model_config = ConfigDict(extra="forbid")

    sha: str
    repo: str
    author: str
    message: str
    committed_at: datetime


def _summarize(body: str | None) -> str:
    """Truncate a PR body to a token-frugal preview.

    Returns the empty string for ``None``/empty input. Bodies at or below
    :data:`SUMMARY_MAX_LEN` are returned verbatim; longer bodies are sliced and
    suffixed with :data:`TRUNCATION_MARKER`, with the slice length adjusted so
    the final string still satisfies ``len(result) <= SUMMARY_MAX_LEN``.
    """
    text = body or ""
    if len(text) <= SUMMARY_MAX_LEN:
        return text
    return text[: SUMMARY_MAX_LEN - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def _parse_next_link(link_header: str | None) -> str | None:
    """Return the ``rel="next"`` URL from a GitHub ``Link`` header, or ``None``.

    GitHub paginates via RFC-5988 link headers; only the ``next`` relation is
    needed to walk forward.
    """
    if not link_header:
        return None
    match = _LINK_NEXT_RE.search(link_header)
    return match.group(1) if match else None


def _pr_from_payload(repo: str, payload: dict[str, Any]) -> GitHubPR:
    """Decode one PR REST payload into a :class:`GitHubPR`."""
    number = int(payload["number"])
    body = payload.get("body") or ""
    return GitHubPR(
        external_id=f"{repo}#{number}",
        repo=repo,
        number=number,
        merged_sha=payload.get("merge_commit_sha"),
        title=payload.get("title", ""),
        body=body,
        summary=_summarize(body),
        merged_at=payload.get("merged_at"),
        created_at=payload["created_at"],
        state=payload.get("state", "open"),
    )


def _commit_from_payload(repo: str, payload: dict[str, Any]) -> GitHubCommit:
    """Decode one commit REST payload into a :class:`GitHubCommit`."""
    commit = payload.get("commit") or {}
    author = commit.get("author") or {}
    return GitHubCommit(
        sha=payload["sha"],
        repo=repo,
        author=author.get("name", "") or author.get("email", ""),
        message=commit.get("message", ""),
        committed_at=author.get("date") or commit.get("committer", {}).get("date"),
    )


class GitHubConnector:
    """Minimal authenticated REST v3 client for the receipts pipeline.

    Pass a real ``httpx.Client`` for live use or a ``MagicMock`` for tests; the
    connector never constructs network sockets implicitly. All requests carry
    a ``Bearer`` auth header and the GitHub-recommended ``Accept`` MIME so the
    server doesn't fall back to the v2 wire shape.
    """

    def __init__(
        self,
        token: str,
        client: httpx.Client | None = None,
        base_url: str = "https://api.github.com",
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._client = client if client is not None else httpx.Client()

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #

    def fetch_prs(
        self,
        repo: str,
        since: datetime | None = None,
        state: Literal["open", "closed", "all"] = "all",
    ) -> list[GitHubPR]:
        """List pull requests for ``repo``.

        Walks ``Link: rel="next"`` to gather every page. ``since`` is forwarded
        as an ISO-8601 query param; GitHub's list-PRs endpoint accepts it as a
        soft filter on update time.
        """
        url: str | None = f"{self._base_url}/repos/{repo}/pulls"
        params: dict[str, Any] | None = {"state": state, "per_page": 100}
        if since is not None:
            params["since"] = since.isoformat()

        prs: list[GitHubPR] = []
        while url is not None:
            response = self._client.get(url, headers=self._headers(), params=params)
            response.raise_for_status()
            payload = response.json()
            prs.extend(_pr_from_payload(repo, item) for item in payload)
            url = _parse_next_link(response.headers.get("Link"))
            # Subsequent pages embed the cursor in the next URL; sending the
            # original params again would double-encode and confuse the server.
            params = None
        return prs

    def fetch_commits_for_pr(self, repo: str, pr_number: int) -> list[GitHubCommit]:
        """List commits attached to ``repo``'s pull request ``pr_number``."""
        url = f"{self._base_url}/repos/{repo}/pulls/{pr_number}/commits"
        response = self._client.get(url, headers=self._headers(), params={"per_page": 100})
        response.raise_for_status()
        payload = response.json()
        return [_commit_from_payload(repo, item) for item in payload]

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #

    def create_pull_request(
        self,
        repo: str,
        title: str,
        body: str,
        base: str,
        head: str,
    ) -> str:
        """POST a new PR to ``repo`` and return the resulting ``html_url``.

        The caller is responsible for ensuring ``head`` already exists on the
        remote — this method only opens the PR.
        """
        url = f"{self._base_url}/repos/{repo}/pulls"
        response = self._client.post(
            url,
            headers=self._headers(),
            json={"title": title, "body": body, "base": base, "head": head},
        )
        response.raise_for_status()
        payload = response.json()
        return payload["html_url"]

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
