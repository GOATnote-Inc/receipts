"""Scribe connector interface + Ambience Healthcare implementation (P2-2).

The Clinical Audit Ledger consumes encounters from an AI scribe vendor:
each encounter generates a sequence of artifact *versions* (raw audio →
transcript → AI-drafted note → clinician-edited note → committed note →
downstream orders) that the receipts pipeline reconciles against what
was actually attested in the EHR.

This module defines the read surface as an :class:`abc.ABC` so additional
vendors (DAX, Suki, Heidi, etc.) can be plugged in without changing the
reconciler, and ships the first concrete implementation against the
Ambience Healthcare REST API.

PHI rules
---------

Plaintext patient identifiers must NEVER reach the receipts pipeline.

  * :class:`ScribeEncounter` carries ``patient_id_hash`` only.
  * Upstream EHRs that already hash their identifiers stay opaque to this
    connector: the hash flows through unchanged.
  * If an Ambience response contains a raw ``patient_id`` (no
    ``patient_id_hash``), this connector hashes it SHA-256 hex on ingest
    before constructing the pydantic model. The raw value is never
    persisted on the model and never logged.

Inferred Ambience wire shape (May 2026):

    GET {base_url}/v1/encounters[?since=<iso8601>]
        → [ScribeEncounter, ...]

    GET {base_url}/v1/encounters/{encounter_id}/artifacts
        → [ScribeArtifactVersion, ...]   (any order; we sort on read)

Auth is a Bearer token in the ``Authorization`` header. The httpx client is
caller-supplied so tests inject a ``MagicMock`` and CI stays hermetic.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

#: Closed set of artifact kinds the receipts pipeline understands. New kinds
#: require a deliberate schema change (and a migration for any persisted rows)
#: so the closed Literal is enforced by pydantic at the boundary.
ArtifactKind = Literal[
    "audio",
    "transcript",
    "ai_note",
    "edited_note",
    "committed_note",
    "order",
]


class ScribeEncounter(BaseModel):
    """A scribe encounter as the receipts pipeline sees it.

    ``patient_id_hash`` is the SHA-256 hex digest of the upstream patient
    identifier and MUST never be a plaintext MRN. See module docstring for
    the PHI contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    external_id: str
    patient_id_hash: str
    started_at: datetime
    chief_complaint: str
    status: str


class ScribeArtifactVersion(BaseModel):
    """One immutable version of a scribe artifact attached to an encounter.

    ``content_ref`` is an opaque pointer (URL / object-store key / path) — the
    receipts pipeline never stores the body inline; that lives in the L5
    Object Lock store. ``content_hash`` lets the ledger detect drift without
    needing to re-fetch the payload.

    ``parent_version`` is the version this one was derived from (e.g. an
    ``edited_note`` typically points back to the ``ai_note`` it revised), or
    ``None`` for roots (raw audio).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    encounter_external_id: str
    kind: ArtifactKind
    content_ref: str
    content_hash: str
    version: int
    parent_version: int | None


class ScribeConnector(ABC):
    """Abstract read surface every scribe-vendor adapter must implement.

    The clinical reconciler only depends on this interface; vendor specifics
    (REST shape, auth, pagination) live in concrete subclasses. The interface
    is deliberately small so adding a vendor stays a one-file change.
    """

    @abstractmethod
    def fetch_encounters(self, since: datetime | None = None) -> list[ScribeEncounter]:
        """Return encounters, optionally filtered to those started after ``since``."""

    @abstractmethod
    def fetch_encounter_versions(self, encounter_id: str) -> list[ScribeArtifactVersion]:
        """Return every artifact version for ``encounter_id`` sorted ascending by ``version``."""


def _hash_patient_id(raw: str) -> str:
    """Return the canonical SHA-256 hex digest used as ``patient_id_hash``."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _encounter_from_payload(payload: dict[str, Any]) -> ScribeEncounter:
    """Decode one encounter payload into a :class:`ScribeEncounter`.

    Honors the PHI contract: if the payload still carries a raw ``patient_id``,
    we hash it locally before constructing the model and drop the plaintext.
    """
    # ``dict(payload)`` so we don't mutate the caller's dict on retry.
    data = dict(payload)
    raw_pid = data.pop("patient_id", None)
    if "patient_id_hash" not in data and raw_pid is not None:
        data["patient_id_hash"] = _hash_patient_id(str(raw_pid))
    return ScribeEncounter.model_validate(data)


class AmbienceScribeConnector(ScribeConnector):
    """REST adapter for the Ambience Healthcare scribe API.

    Pass a real ``httpx.Client`` for live use or a ``MagicMock`` for tests.
    The connector never constructs network sockets implicitly through any
    request method beyond what the injected client does. Auth is a Bearer
    token on every request; we do not stash it on the httpx client because
    callers may legitimately reuse one client across connectors with
    distinct keys.
    """

    DEFAULT_BASE_URL = "https://api.ambiencehealthcare.com"

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

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #

    def fetch_encounters(self, since: datetime | None = None) -> list[ScribeEncounter]:
        """Return encounters from Ambience.

        ``since`` is serialized as ISO 8601 with timezone offset preserved and
        forwarded as the ``since`` query parameter.
        """
        params: dict[str, str] = {}
        if since is not None:
            params["since"] = since.isoformat()

        url = f"{self._base_url}/v1/encounters"
        response = self._client.get(url, headers=self._auth_headers(), params=params)
        response.raise_for_status()
        payload = response.json()
        return [_encounter_from_payload(item) for item in payload]

    def fetch_encounter_versions(self, encounter_id: str) -> list[ScribeArtifactVersion]:
        """Return every artifact version for ``encounter_id``, sorted ascending."""
        if not encounter_id:
            raise ValueError("encounter_id is required")
        url = f"{self._base_url}/v1/encounters/{encounter_id}/artifacts"
        response = self._client.get(url, headers=self._auth_headers())
        response.raise_for_status()
        payload = response.json()
        versions = [ScribeArtifactVersion.model_validate(item) for item in payload]
        versions.sort(key=lambda v: v.version)
        return versions

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}
