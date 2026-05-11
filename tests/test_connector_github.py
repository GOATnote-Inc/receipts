"""Tests for the GitHub REST v3 connector (P1-2).

The connector wraps a small slice of the GitHub REST API: list PRs in a repo,
list commits attached to a PR, and create a new PR. All calls are exercised
through an injected ``httpx.Client`` mock — no real network. Response shapes
mirror the real API just enough that the parsing surface is honest.

Contracts asserted:
  (a) ``fetch_prs`` follows ``Link: rel=next`` pagination, concatenating pages.
  (b) ``fetch_prs`` forwards ``state`` to the API as a query param.
  (c) ``fetch_commits_for_pr`` decodes one page of commits into ``GitHubCommit``.
  (d) ``create_pull_request`` posts to ``/repos/{repo}/pulls`` with the expected
      JSON body and returns the new PR's ``html_url``.
  (e) ``GitHubPR.external_id`` is rendered as ``"owner/repo#number"``.
  (f) ``GitHubPR.summary`` is the body truncated to ≤500 chars with an ellipsis
      suffix when truncation actually happened.

Auth header (``Authorization: Bearer <token>``) and the ``Accept`` MIME are
asserted in the create-PR test as a smoke check; the same headers are used by
the read methods through the same client.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from receipts.connectors import GitHubCommit, GitHubConnector, GitHubPR

# ---------------------------------------------------------------------------
# REST response fixtures
# ---------------------------------------------------------------------------


def _pr_payload(
    *,
    number: int,
    title: str = "Sample PR",
    body: str = "Sample body",
    state: str = "open",
    merged_at: str | None = None,
    merge_commit_sha: str | None = None,
    created_at: str = "2026-04-01T10:00:00Z",
) -> dict:
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "merged_at": merged_at,
        "merge_commit_sha": merge_commit_sha,
        "created_at": created_at,
        "html_url": f"https://github.com/owner/repo/pull/{number}",
    }


def _commit_payload(*, sha: str, author: str, message: str, date: str) -> dict:
    return {
        "sha": sha,
        "commit": {
            "author": {"name": author, "email": f"{author}@example.com", "date": date},
            "message": message,
        },
    }


def _mock_response(*, status: int, json_body, headers: dict[str, str] | None = None) -> MagicMock:
    """Return a MagicMock that quacks like an ``httpx.Response``."""
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_body
    resp.headers = headers or {}
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# fetch_prs
# ---------------------------------------------------------------------------


def test_fetch_prs_paginates_and_combines() -> None:
    """Two pages stitched via ``Link: rel=next`` should yield one combined list."""
    page1 = [_pr_payload(number=1), _pr_payload(number=2)]
    page2 = [_pr_payload(number=3)]

    next_url = "https://api.github.com/repositories/1/pulls?page=2&state=all"
    page1_headers = {"Link": f'<{next_url}>; rel="next", <…>; rel="last"'}

    client = MagicMock()
    client.get.side_effect = [
        _mock_response(status=200, json_body=page1, headers=page1_headers),
        _mock_response(status=200, json_body=page2, headers={}),
    ]

    conn = GitHubConnector(token="t0k3n", client=client)
    prs = conn.fetch_prs("owner/repo")

    assert [pr.number for pr in prs] == [1, 2, 3]
    assert all(isinstance(pr, GitHubPR) for pr in prs)
    assert client.get.call_count == 2

    first_call = client.get.call_args_list[0]
    assert first_call.args[0] == "https://api.github.com/repos/owner/repo/pulls"
    assert first_call.kwargs["params"]["state"] == "all"

    second_call = client.get.call_args_list[1]
    assert second_call.args[0] == next_url


def test_fetch_prs_filters_by_state() -> None:
    """``state="closed"`` must reach the API as a query parameter."""
    client = MagicMock()
    client.get.return_value = _mock_response(
        status=200,
        json_body=[_pr_payload(number=7, state="closed", merged_at="2026-04-05T12:00:00Z")],
        headers={},
    )

    conn = GitHubConnector(token="t0k3n", client=client)
    prs = conn.fetch_prs("owner/repo", state="closed")

    assert len(prs) == 1
    assert prs[0].state == "closed"

    params = client.get.call_args.kwargs["params"]
    assert params["state"] == "closed"


def test_fetch_prs_forwards_since() -> None:
    """``since`` becomes an ISO 8601 query param the API understands."""
    client = MagicMock()
    client.get.return_value = _mock_response(status=200, json_body=[], headers={})

    conn = GitHubConnector(token="t0k3n", client=client)
    since = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    conn.fetch_prs("owner/repo", since=since)

    params = client.get.call_args.kwargs["params"]
    assert params["since"] == "2026-04-01T00:00:00+00:00"


def test_fetch_commits_for_pr_returns_list() -> None:
    """Commit payloads should decode into ``GitHubCommit`` values."""
    payload = [
        _commit_payload(
            sha="deadbeef",
            author="alice",
            message="first",
            date="2026-04-01T09:00:00Z",
        ),
        _commit_payload(
            sha="cafebabe",
            author="bob",
            message="second",
            date="2026-04-02T09:00:00Z",
        ),
    ]
    client = MagicMock()
    client.get.return_value = _mock_response(status=200, json_body=payload, headers={})

    conn = GitHubConnector(token="t0k3n", client=client)
    commits = conn.fetch_commits_for_pr("owner/repo", 42)

    assert [c.sha for c in commits] == ["deadbeef", "cafebabe"]
    assert all(isinstance(c, GitHubCommit) for c in commits)
    assert commits[0].author == "alice"
    assert commits[0].message == "first"
    assert commits[0].repo == "owner/repo"

    url = client.get.call_args.args[0]
    assert url == "https://api.github.com/repos/owner/repo/pulls/42/commits"


def test_create_pull_request_posts_correctly() -> None:
    """``create_pull_request`` posts the right body and returns ``html_url``."""
    html_url = "https://github.com/owner/repo/pull/99"
    client = MagicMock()
    client.post.return_value = _mock_response(
        status=201,
        json_body={"number": 99, "html_url": html_url},
        headers={},
    )

    conn = GitHubConnector(token="t0k3n", client=client)
    result = conn.create_pull_request(
        "owner/repo",
        title="Add receipts ledger",
        body="Closes #1",
        base="main",
        head="feature/ledger",
    )

    assert result == html_url

    call = client.post.call_args
    assert call.args[0] == "https://api.github.com/repos/owner/repo/pulls"
    sent = call.kwargs["json"]
    assert sent == {
        "title": "Add receipts ledger",
        "body": "Closes #1",
        "base": "main",
        "head": "feature/ledger",
    }
    headers = call.kwargs["headers"]
    assert headers["Authorization"] == "Bearer t0k3n"
    assert headers["Accept"] == "application/vnd.github+json"


def test_pr_external_id_format() -> None:
    """``external_id`` is rendered as ``"<repo>#<number>"`` regardless of source."""
    client = MagicMock()
    client.get.return_value = _mock_response(
        status=200,
        json_body=[_pr_payload(number=42)],
        headers={},
    )
    conn = GitHubConnector(token="t0k3n", client=client)
    [pr] = conn.fetch_prs("owner/repo")

    assert pr.external_id == "owner/repo#42"


def test_summary_truncates_long_body() -> None:
    """A body over 500 characters is truncated and ellipsized in ``summary``."""
    long_body = "x" * 800
    client = MagicMock()
    client.get.return_value = _mock_response(
        status=200,
        json_body=[_pr_payload(number=12, body=long_body)],
        headers={},
    )
    conn = GitHubConnector(token="t0k3n", client=client)
    [pr] = conn.fetch_prs("owner/repo")

    assert pr.body == long_body
    assert len(pr.summary) <= 500
    assert pr.summary.endswith("…")
    assert pr.summary.startswith("x" * 100)


def test_summary_preserves_short_body() -> None:
    """A body at or under 500 characters survives unchanged in ``summary``."""
    short_body = "all done"
    client = MagicMock()
    client.get.return_value = _mock_response(
        status=200,
        json_body=[_pr_payload(number=13, body=short_body)],
        headers={},
    )
    conn = GitHubConnector(token="t0k3n", client=client)
    [pr] = conn.fetch_prs("owner/repo")

    assert pr.summary == short_body
