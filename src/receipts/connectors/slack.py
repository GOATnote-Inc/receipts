"""Slack connector — read channel threads, send Block Kit DMs.

Wraps three Slack Web API methods:

  - ``conversations.history``  : list root messages in a channel
  - ``conversations.replies``  : expand a single thread (when ``thread_ts`` set)
  - ``conversations.open``     : resolve a user_id → DM channel
  - ``chat.postMessage``       : post a Block Kit message

The connector returns ``SlackThread`` — one per logical thread in the channel
(threaded conversations are folded; standalone messages thread under their
own ``ts``). The ``external_id`` is ``"<channel>:<thread_ts>"`` so the
ledger and reconciler can address threads without collision.

Real Slack is never called from tests. Inject a MagicMock ``httpx.Client``
via the constructor's ``client`` kwarg.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

__all__ = ["SlackConnector", "SlackThread"]


class SlackThread(BaseModel):
    """A Slack thread as it appears to the Engineering Receipts pipeline.

    ``external_id`` is a deterministic ``"<channel>:<thread_ts>"`` key.
    ``summary`` is the root text plus reply bodies joined by a newline so
    downstream drafters see context, not just the root message.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    external_id: str
    channel: str
    root_user: str
    text: str
    summary: str
    reply_count: int = Field(ge=0)
    last_message_at: datetime


def _ts_to_datetime(ts: str) -> datetime:
    """Convert a Slack ``ts`` (epoch-seconds float as string) to aware UTC."""
    return datetime.fromtimestamp(float(ts), tz=UTC)


class SlackConnector:
    """Thin Slack Web API wrapper.

    Parameters
    ----------
    bot_token:
        Bot user OAuth token (``xoxb-...``). Sent as ``Authorization: Bearer``.
    client:
        Optional pre-built ``httpx.Client``. If omitted, the connector builds
        one with a 10s timeout. Tests pass a MagicMock here.
    base_url:
        Slack Web API base. Override only for tests against fakes.
    """

    def __init__(
        self,
        bot_token: str,
        client: httpx.Client | None = None,
        base_url: str = "https://slack.com/api",
    ) -> None:
        self._token = bot_token
        self._base_url = base_url.rstrip("/")
        self._client = client if client is not None else httpx.Client(timeout=10.0)

    # ------------------------------------------------------------------
    # Low-level call
    # ------------------------------------------------------------------

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST to ``<base_url>/<method>`` with bot-token auth.

        Slack accepts both form-encoded and JSON; JSON is required when the
        body contains nested structures (``blocks``). We use JSON for
        ``chat.postMessage`` and form for everything else — matching the
        documented patterns and keeping request shapes legible in tests.
        """

        url = f"{self._base_url}/{method}"
        headers = {
            "Authorization": f"Bearer {self._token}",
        }
        if method == "chat.postMessage":
            headers["Content-Type"] = "application/json; charset=utf-8"
            response = self._client.post(url, headers=headers, json=payload)
        else:
            response = self._client.post(url, headers=headers, data=payload)
        response.raise_for_status()
        body = response.json()
        if not body.get("ok", False):
            raise RuntimeError(f"Slack API {method} failed: {body.get('error', body)}")
        return body

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def fetch_channel_messages(
        self,
        channel_id: str,
        since: datetime | None = None,
    ) -> list[SlackThread]:
        """List channel threads since ``since`` (or all-time if ``None``).

        Implementation: pull ``conversations.history`` once, then for every
        root message whose ``reply_count > 0`` pull ``conversations.replies``
        to gather the full body for the summary. Standalone messages (no
        replies) thread under their own ``ts``.
        """

        history_payload: dict[str, Any] = {"channel": channel_id, "limit": 200}
        if since is not None:
            history_payload["oldest"] = f"{since.timestamp():.6f}"

        history = self._call("conversations.history", history_payload)
        roots = [m for m in history.get("messages", []) if m.get("type", "message") == "message"]

        threads: list[SlackThread] = []
        for root in roots:
            ts = root["ts"]
            thread_ts = root.get("thread_ts", ts)
            reply_count = int(root.get("reply_count", 0))

            if reply_count > 0:
                replies = self._call(
                    "conversations.replies",
                    {"channel": channel_id, "ts": thread_ts, "limit": 200},
                )
                reply_msgs = replies.get("messages", [])
                # conversations.replies returns the root as the first element.
                bodies = [m.get("text", "") for m in reply_msgs]
                summary = "\n".join(bodies)
                last_ts = max((m.get("ts", thread_ts) for m in reply_msgs), key=float)
                # Slack's reply_count excludes the root; preserve that.
                non_root = [m for m in reply_msgs if m.get("ts") != thread_ts]
                effective_reply_count = len(non_root) if non_root else reply_count
            else:
                summary = root.get("text", "")
                last_ts = root.get("latest_reply", ts)
                effective_reply_count = 0

            threads.append(
                SlackThread(
                    external_id=f"{channel_id}:{thread_ts}",
                    channel=channel_id,
                    root_user=root.get("user", ""),
                    text=root.get("text", ""),
                    summary=summary,
                    reply_count=effective_reply_count,
                    last_message_at=_ts_to_datetime(last_ts),
                )
            )

        return threads

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def send_dm(self, user_id: str, blocks: list[dict]) -> str:
        """Open a DM channel with ``user_id`` and post ``blocks``.

        Returns the ``ts`` of the posted message — the canonical Slack
        receipt for ledger anchoring.
        """

        opened = self._call("conversations.open", {"users": user_id})
        dm_channel = opened["channel"]["id"]
        posted = self._call(
            "chat.postMessage",
            {"channel": dm_channel, "blocks": blocks},
        )
        return posted["ts"]
