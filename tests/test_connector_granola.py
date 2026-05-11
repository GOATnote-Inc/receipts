"""Tests for the Granola connector (P1-4).

Granola is a meeting-notes / decision-extraction product. Its public REST API
shape is not fully documented as of May 2026, so this connector targets a
reasonable inferred surface:

  - ``GET /api/v1/meetings`` (optional ``since=<iso8601>`` filter)
  - ``GET /api/v1/meetings/{meeting_id}/decisions``

``base_url`` is configurable so on-prem or staging deployments can swap it.
Auth is a Bearer token in the ``Authorization`` header.

These tests use ``MagicMock`` httpx clients. We never hit the real Granola
API — that's a hard rule for connector unit tests, both to keep CI hermetic
and to avoid leaking the (hypothetical) API key.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from receipts.connectors import (
    GranolaConnector,
    GranolaDecision,
    GranolaMeeting,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(json_payload: object, status_code: int = 200) -> MagicMock:
    """Build a MagicMock that quacks like an httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_payload
    resp.raise_for_status.return_value = None
    return resp


def _meeting_payload(
    *,
    external_id: str = "MTG-001",
    title: str = "Sprint planning",
    started_at: str = "2026-05-01T15:00:00+00:00",
    transcript_ref: str = "https://granola.ai/t/MTG-001",
    attendees: list[str] | None = None,
    duration_min: int = 45,
) -> dict[str, object]:
    return {
        "external_id": external_id,
        "title": title,
        "started_at": started_at,
        "transcript_ref": transcript_ref,
        "attendees": attendees if attendees is not None else ["alice@x.io", "bob@x.io"],
        "duration_min": duration_min,
    }


def _decision_payload(
    *,
    meeting_id: str = "MTG-001",
    decision_text: str = "Defer EPIC-7 to next sprint.",
    tagged_epic_external_ids: list[str] | None = None,
    confidence: float = 0.82,
) -> dict[str, object]:
    return {
        "meeting_id": meeting_id,
        "decision_text": decision_text,
        "tagged_epic_external_ids": (
            tagged_epic_external_ids if tagged_epic_external_ids is not None else ["EPIC-7"]
        ),
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# fetch_meetings
# ---------------------------------------------------------------------------


def test_fetch_meetings_returns_parsed_list() -> None:
    """GET /api/v1/meetings → list[GranolaMeeting] with all fields parsed."""
    client = MagicMock()
    client.get.return_value = _make_response(
        {
            "meetings": [
                _meeting_payload(external_id="MTG-001", title="Sprint planning"),
                _meeting_payload(
                    external_id="MTG-002",
                    title="Architecture review",
                    started_at="2026-05-02T17:30:00+00:00",
                    duration_min=60,
                ),
            ]
        }
    )

    connector = GranolaConnector(api_key="sk-test", client=client)
    meetings = connector.fetch_meetings()

    assert isinstance(meetings, list)
    assert len(meetings) == 2
    assert all(isinstance(m, GranolaMeeting) for m in meetings)
    assert meetings[0].external_id == "MTG-001"
    assert meetings[0].title == "Sprint planning"
    assert meetings[0].duration_min == 45
    assert meetings[0].attendees == ["alice@x.io", "bob@x.io"]
    assert meetings[1].external_id == "MTG-002"
    assert meetings[1].duration_min == 60

    # URL is built off the (default) base_url.
    called_url = client.get.call_args.args[0]
    assert called_url.endswith("/api/v1/meetings")


def test_fetch_meetings_filters_by_since() -> None:
    """``since`` kwarg is passed as ``?since=<iso8601>`` query param."""
    client = MagicMock()
    client.get.return_value = _make_response({"meetings": []})

    connector = GranolaConnector(api_key="sk-test", client=client)
    cutoff = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    connector.fetch_meetings(since=cutoff)

    call = client.get.call_args
    params = call.kwargs.get("params") or {}
    assert "since" in params
    # ISO 8601 with timezone preserved.
    assert params["since"].startswith("2026-05-01T00:00:00")
    assert "+00:00" in params["since"] or params["since"].endswith("Z")


def test_fetch_meetings_omits_since_when_none() -> None:
    """No ``since`` → no ``since`` query param sent."""
    client = MagicMock()
    client.get.return_value = _make_response({"meetings": []})

    connector = GranolaConnector(api_key="sk-test", client=client)
    connector.fetch_meetings(since=None)

    params = client.get.call_args.kwargs.get("params") or {}
    assert "since" not in params


# ---------------------------------------------------------------------------
# fetch_meeting_decisions
# ---------------------------------------------------------------------------


def test_fetch_meeting_decisions_extracts_epic_tags() -> None:
    """Decisions parse ``tagged_epic_external_ids`` from response payload."""
    client = MagicMock()
    client.get.return_value = _make_response(
        {
            "decisions": [
                _decision_payload(
                    meeting_id="MTG-007",
                    decision_text="Drop OAuth from Epic 7; ship API-key only.",
                    tagged_epic_external_ids=["EPIC-7", "EPIC-11"],
                    confidence=0.91,
                ),
                _decision_payload(
                    meeting_id="MTG-007",
                    decision_text="Move auth deep-dive to next sprint.",
                    tagged_epic_external_ids=[],
                    confidence=0.55,
                ),
            ]
        }
    )

    connector = GranolaConnector(api_key="sk-test", client=client)
    decisions = connector.fetch_meeting_decisions("MTG-007")

    assert len(decisions) == 2
    assert all(isinstance(d, GranolaDecision) for d in decisions)
    assert decisions[0].tagged_epic_external_ids == ["EPIC-7", "EPIC-11"]
    assert decisions[0].confidence == pytest.approx(0.91)
    assert decisions[1].tagged_epic_external_ids == []
    assert decisions[1].meeting_id == "MTG-007"

    # URL embeds the meeting id.
    called_url = client.get.call_args.args[0]
    assert called_url.endswith("/api/v1/meetings/MTG-007/decisions")


# ---------------------------------------------------------------------------
# Round-trip + auth
# ---------------------------------------------------------------------------


def test_meeting_external_id_round_trips() -> None:
    """External id from response payload is preserved verbatim on the model."""
    client = MagicMock()
    client.get.return_value = _make_response(
        {"meetings": [_meeting_payload(external_id="granola_meeting_abc123")]}
    )

    connector = GranolaConnector(api_key="sk-test", client=client)
    meetings = connector.fetch_meetings()

    assert meetings[0].external_id == "granola_meeting_abc123"
    # Model JSON round-trip preserves the id, started_at, and duration.
    dumped = meetings[0].model_dump()
    assert dumped["external_id"] == "granola_meeting_abc123"
    assert dumped["duration_min"] == 45


def test_authorization_header_includes_api_key() -> None:
    """Constructor wires the api_key into a ``Bearer`` Authorization header."""
    client = MagicMock()
    client.get.return_value = _make_response({"meetings": []})

    connector = GranolaConnector(api_key="sk-super-secret", client=client)
    connector.fetch_meetings()

    headers = client.get.call_args.kwargs.get("headers") or {}
    assert headers.get("Authorization") == "Bearer sk-super-secret"


def test_custom_base_url_is_honored() -> None:
    """Custom base_url replaces the default in outgoing URLs."""
    client = MagicMock()
    client.get.return_value = _make_response({"meetings": []})

    connector = GranolaConnector(
        api_key="sk-test",
        client=client,
        base_url="https://granola.internal.example.com",
    )
    connector.fetch_meetings()

    called_url = client.get.call_args.args[0]
    assert called_url == "https://granola.internal.example.com/api/v1/meetings"
