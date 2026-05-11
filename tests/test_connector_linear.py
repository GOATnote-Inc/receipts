"""Tests for the Linear connector (P1-1).

The Linear connector is the first Engineering Receipts data source. It must
NEVER make a real network call from the test suite -- every test injects a
``MagicMock`` ``httpx.Client`` and hand-crafts the GraphQL response shape.

Why this matters
----------------
- ``make test`` must remain hermetic: no API keys, no network, no rate limits.
- Schema drift in the Linear GraphQL surface should be caught by the unit
  tests' explicit response fixtures, not by silent passthrough behaviour.
- Acceptance-criteria parsing is a load-bearing input into the drafter
  (P1-5) and the CEIS judge stack, so both numbered and bulleted list
  shapes need explicit coverage here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from receipts.connectors import LinearConnector, LinearEpic


def _mock_client_with_response(payload: dict) -> MagicMock:
    """Return a MagicMock httpx.Client whose .post() yields a response with .json() == payload."""
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    client = MagicMock()
    client.post.return_value = response
    return client


def _epic_node(
    external_id: str,
    title: str,
    description: str,
    state: str = "In Progress",
    created_at: str = "2026-05-01T00:00:00.000Z",
    updated_at: str = "2026-05-02T00:00:00.000Z",
) -> dict:
    return {
        "id": external_id,
        "title": title,
        "description": description,
        "state": {"name": state},
        "createdAt": created_at,
        "updatedAt": updated_at,
    }


# --------------------------- fetch_epics ---------------------------


def test_fetch_epics_returns_parsed_list() -> None:
    payload = {
        "data": {
            "team": {
                "projects": {
                    "nodes": [
                        _epic_node("ext-1", "Alpha", "Goal A\n\n1. AC one\n2. AC two"),
                        _epic_node("ext-2", "Beta", "Goal B\n\n- bullet one\n- bullet two"),
                        _epic_node("ext-3", "Gamma", "no criteria here"),
                    ]
                }
            }
        }
    }
    client = _mock_client_with_response(payload)
    conn = LinearConnector(api_key="sk-test", client=client)

    result = conn.fetch_epics(team_id="team-xyz")

    assert isinstance(result, list)
    assert len(result) == 3
    assert all(isinstance(e, LinearEpic) for e in result)
    assert [e.external_id for e in result] == ["ext-1", "ext-2", "ext-3"]
    assert [e.title for e in result] == ["Alpha", "Beta", "Gamma"]
    assert result[0].state == "In Progress"
    assert isinstance(result[0].created_at, datetime)
    assert result[0].created_at.tzinfo is not None
    assert result[0].acceptance_criteria_parsed == ["AC one", "AC two"]
    assert result[1].acceptance_criteria_parsed == ["bullet one", "bullet two"]
    assert result[2].acceptance_criteria_parsed == []

    # POST went to the GraphQL endpoint with Bearer auth.
    args, kwargs = client.post.call_args
    assert args[0] == "https://api.linear.app/graphql"
    headers = kwargs.get("headers", {})
    assert headers.get("Authorization") == "Bearer sk-test"
    json_body = kwargs.get("json", {})
    assert "query" in json_body
    assert json_body.get("variables", {}).get("teamId") == "team-xyz"


def test_fetch_epics_passes_since_variable() -> None:
    payload = {"data": {"team": {"projects": {"nodes": []}}}}
    client = _mock_client_with_response(payload)
    conn = LinearConnector(api_key="sk-test", client=client)

    since = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    result = conn.fetch_epics(team_id="team-xyz", since=since)

    assert result == []
    _, kwargs = client.post.call_args
    variables = kwargs["json"]["variables"]
    assert variables["teamId"] == "team-xyz"
    # since is serialised as ISO-8601 with explicit Z/UTC offset
    assert variables.get("since", "").startswith("2026-05-01T12:00:00")


# --------------------------- fetch_epic_by_id ---------------------------


def test_fetch_epic_by_id_found() -> None:
    payload = {
        "data": {
            "project": _epic_node(
                "ext-42",
                "The Answer",
                "Body\n\n1) first\n2) second\n3) third",
            )
        }
    }
    client = _mock_client_with_response(payload)
    conn = LinearConnector(api_key="sk-test", client=client)

    epic = conn.fetch_epic_by_id("ext-42")

    assert epic is not None
    assert isinstance(epic, LinearEpic)
    assert epic.external_id == "ext-42"
    assert epic.title == "The Answer"
    assert epic.acceptance_criteria_parsed == ["first", "second", "third"]


def test_fetch_epic_by_id_not_found() -> None:
    # Linear returns errors array + null data when the id is unknown.
    payload = {
        "data": {"project": None},
        "errors": [{"message": "Entity not found", "extensions": {"code": "NOT_FOUND"}}],
    }
    client = _mock_client_with_response(payload)
    conn = LinearConnector(api_key="sk-test", client=client)

    epic = conn.fetch_epic_by_id("does-not-exist")

    assert epic is None


# --------------------------- add_comment ---------------------------


def test_add_comment_posts_to_correct_endpoint() -> None:
    payload = {
        "data": {
            "commentCreate": {
                "success": True,
                "comment": {"id": "comment-123"},
            }
        }
    }
    client = _mock_client_with_response(payload)
    conn = LinearConnector(api_key="sk-test", client=client)

    comment_id = conn.add_comment(epic_external_id="ext-1", body="LGTM ship it")

    assert comment_id == "comment-123"
    args, kwargs = client.post.call_args
    assert args[0] == "https://api.linear.app/graphql"
    json_body = kwargs["json"]
    assert "mutation" in json_body["query"].lower()
    assert "commentCreate" in json_body["query"]
    variables = json_body["variables"]
    assert variables["projectId"] == "ext-1"
    assert variables["body"] == "LGTM ship it"
    headers = kwargs["headers"]
    assert headers["Authorization"] == "Bearer sk-test"


# --------------------------- acceptance criteria parsing ---------------------------


def test_acceptance_criteria_parsed_from_numbered_list() -> None:
    desc = (
        "## Goal\n"
        "Ship the connector.\n\n"
        "## Acceptance Criteria\n"
        "1. fetch_epics returns parsed list\n"
        "2. fetch_epic_by_id handles 404\n"
        "3) add_comment posts mutation\n"
    )
    payload = {"data": {"project": _epic_node("ext-9", "Connector", desc)}}
    client = _mock_client_with_response(payload)
    conn = LinearConnector(api_key="sk-test", client=client)

    epic = conn.fetch_epic_by_id("ext-9")

    assert epic is not None
    assert epic.acceptance_criteria_parsed == [
        "fetch_epics returns parsed list",
        "fetch_epic_by_id handles 404",
        "add_comment posts mutation",
    ]


def test_acceptance_criteria_parsed_from_bullets() -> None:
    desc = (
        "Intro line.\n\n"
        "Acceptance:\n"
        "- **fast** path covered\n"
        "- _slow_ path covered\n"
        "* edge case covered\n"
    )
    payload = {"data": {"project": _epic_node("ext-10", "Bullets", desc)}}
    client = _mock_client_with_response(payload)
    conn = LinearConnector(api_key="sk-test", client=client)

    epic = conn.fetch_epic_by_id("ext-10")

    assert epic is not None
    # markdown emphasis stripped
    assert epic.acceptance_criteria_parsed == [
        "fast path covered",
        "slow path covered",
        "edge case covered",
    ]
