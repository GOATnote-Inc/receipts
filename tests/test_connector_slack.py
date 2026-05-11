"""Tests for the Slack connector (P1-3).

The connector wraps a small subset of the Slack Web API:

  - ``conversations.history`` to pull root messages in a channel
  - ``conversations.replies`` to expand each rooted thread
  - ``conversations.open`` to resolve a user_id → DM channel
  - ``chat.postMessage`` to send Block Kit DMs

The connector is the read-side input to the Engineering Receipts pipeline
(``ThreadRef`` execution context) and the write-side output of the weekly
digest. Real Slack is never called from tests — every test injects a
``MagicMock`` ``httpx.Client`` and asserts both the request payloads and
the parsed return shape.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from receipts.connectors import SlackConnector, SlackThread

# ---------------------------------------------------------------------------
# Helpers — realistic Slack Web API response shapes
# ---------------------------------------------------------------------------


def _make_response(payload: dict) -> MagicMock:
    """Build a MagicMock that mimics httpx.Response for a Slack Web API call."""

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


def _history_payload() -> dict:
    """Two channel root messages — one with replies, one standalone."""
    return {
        "ok": True,
        "messages": [
            {
                "type": "message",
                "user": "U_ROOT_A",
                "text": "Anyone else seeing the staging 500s on /v1/spec?",
                "ts": "1700000100.000100",
                "thread_ts": "1700000100.000100",
                "reply_count": 2,
                "latest_reply": "1700000300.000400",
            },
            {
                "type": "message",
                "user": "U_ROOT_B",
                "text": "Standup notes are in the doc.",
                "ts": "1700000500.000500",
            },
        ],
        "has_more": False,
    }


def _replies_payload() -> dict:
    """Three messages: root + two replies for thread_ts=1700000100.000100."""
    return {
        "ok": True,
        "messages": [
            {
                "type": "message",
                "user": "U_ROOT_A",
                "text": "Anyone else seeing the staging 500s on /v1/spec?",
                "ts": "1700000100.000100",
                "thread_ts": "1700000100.000100",
            },
            {
                "type": "message",
                "user": "U_REPLY_A",
                "text": "Yes — looks like the new auth middleware is rejecting service tokens.",
                "ts": "1700000200.000200",
                "thread_ts": "1700000100.000100",
            },
            {
                "type": "message",
                "user": "U_REPLY_B",
                "text": "Rolling back PR-101 now.",
                "ts": "1700000300.000400",
                "thread_ts": "1700000100.000100",
            },
        ],
        "has_more": False,
    }


def _make_client(*responses: MagicMock) -> MagicMock:
    """Build a MagicMock httpx.Client whose ``.post(...)`` returns ``responses`` in order."""
    client = MagicMock()
    client.post = MagicMock(side_effect=list(responses))
    return client


# ---------------------------------------------------------------------------
# fetch_channel_messages
# ---------------------------------------------------------------------------


def test_fetch_channel_messages_groups_threads() -> None:
    """Root + replies must collapse into a single SlackThread per ``thread_ts``."""

    client = _make_client(
        _make_response(_history_payload()),
        _make_response(_replies_payload()),
    )
    conn = SlackConnector(bot_token="xoxb-test", client=client)

    threads = conn.fetch_channel_messages("C123")

    # Two root messages → two SlackThreads. The threaded root gets reply_count=2;
    # the standalone gets reply_count=0.
    assert len(threads) == 2
    threaded = next(t for t in threads if t.root_user == "U_ROOT_A")
    standalone = next(t for t in threads if t.root_user == "U_ROOT_B")

    assert threaded.reply_count == 2
    assert threaded.text.startswith("Anyone else seeing the staging 500s")
    # Summary should fold in at least the root + one reply body so downstream
    # drafters see context, not just the root.
    assert "auth middleware" in threaded.summary or "Rolling back" in threaded.summary
    assert threaded.channel == "C123"

    assert standalone.reply_count == 0
    assert standalone.text == "Standup notes are in the doc."


def test_fetch_channel_messages_filters_by_since() -> None:
    """The ``since`` filter must translate to the ``oldest`` query param (Slack epoch float)."""

    client = _make_client(_make_response({"ok": True, "messages": [], "has_more": False}))
    conn = SlackConnector(bot_token="xoxb-test", client=client)

    since = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    conn.fetch_channel_messages("C123", since=since)

    # Exactly one call (no threads to expand), and it must target conversations.history
    # with channel=C123 and oldest=<unix-seconds>.
    assert client.post.call_count == 1
    call = client.post.call_args
    url = call.args[0] if call.args else call.kwargs.get("url")
    assert "conversations.history" in url
    data = call.kwargs.get("data") or call.kwargs.get("json") or {}
    assert data.get("channel") == "C123"
    assert "oldest" in data
    assert float(data["oldest"]) == pytest.approx(since.timestamp())


# ---------------------------------------------------------------------------
# send_dm
# ---------------------------------------------------------------------------


def test_send_dm_opens_conversation_then_posts() -> None:
    """send_dm must call conversations.open FIRST, then chat.postMessage."""

    open_resp = _make_response({"ok": True, "channel": {"id": "D9999"}})
    post_resp = _make_response({"ok": True, "ts": "1700001234.567890", "channel": "D9999"})
    client = _make_client(open_resp, post_resp)
    conn = SlackConnector(bot_token="xoxb-test", client=client)

    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "weekly receipt"}}]
    conn.send_dm("UABC", blocks)

    assert client.post.call_count == 2
    first_url = client.post.call_args_list[0].args[0]
    second_url = client.post.call_args_list[1].args[0]
    assert "conversations.open" in first_url
    assert "chat.postMessage" in second_url

    # conversations.open must reference the user_id
    first_data = (
        client.post.call_args_list[0].kwargs.get("data")
        or client.post.call_args_list[0].kwargs.get("json")
        or {}
    )
    assert first_data.get("users") == "UABC"

    # chat.postMessage must use the channel id returned by conversations.open
    second_kwargs = client.post.call_args_list[1].kwargs
    second_payload = second_kwargs.get("json") or second_kwargs.get("data") or {}
    assert second_payload.get("channel") == "D9999"
    assert second_payload.get("blocks") == blocks


def test_send_dm_returns_ts() -> None:
    """The function must return the ts of the posted message."""

    open_resp = _make_response({"ok": True, "channel": {"id": "D4242"}})
    post_resp = _make_response({"ok": True, "ts": "1700009999.000111", "channel": "D4242"})
    client = _make_client(open_resp, post_resp)
    conn = SlackConnector(bot_token="xoxb-test", client=client)

    ts = conn.send_dm("U777", [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}])

    assert ts == "1700009999.000111"


# ---------------------------------------------------------------------------
# SlackThread.external_id contract
# ---------------------------------------------------------------------------


def test_thread_external_id_format() -> None:
    """SlackThread.external_id must be the channel + thread_ts joined."""

    client = _make_client(
        _make_response(_history_payload()),
        _make_response(_replies_payload()),
    )
    conn = SlackConnector(bot_token="xoxb-test", client=client)

    threads = conn.fetch_channel_messages("C123")

    # Find the threaded root by its ts/thread_ts.
    threaded = next(t for t in threads if t.root_user == "U_ROOT_A")
    standalone = next(t for t in threads if t.root_user == "U_ROOT_B")

    # Format: "<channel>:<thread_ts>". This is stable, sortable, and round-trips
    # through ThreadRef.external_id without collision.
    assert threaded.external_id == "C123:1700000100.000100"
    # Standalone messages thread under their own ts.
    assert standalone.external_id == "C123:1700000500.000500"


# Ensure the package re-exports the type so other connectors (and the
# reconciler) can `from receipts.connectors import SlackThread`.
def test_slackthread_is_a_pydantic_model() -> None:
    fields = SlackThread.model_fields
    expected = {
        "external_id",
        "channel",
        "root_user",
        "text",
        "summary",
        "reply_count",
        "last_message_at",
    }
    assert expected.issubset(set(fields))
