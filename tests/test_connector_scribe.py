"""Tests for the Scribe connector interface and Ambience implementation (P2-2).

Two layers are exercised:

  (1) ``ScribeConnector`` is an :class:`abc.ABC` describing the read surface the
      clinical reconciler depends on:

          fetch_encounters(since: datetime | None) -> list[ScribeEncounter]
          fetch_encounter_versions(id: str) -> list[ScribeArtifactVersion]

      Instantiating the ABC directly must raise ``TypeError`` so accidental
      "use the abstract class" mistakes fail loudly in CI.

  (2) ``AmbienceScribeConnector`` is the first concrete implementation, wrapping
      the (inferred) Ambience Healthcare REST surface:

          GET {base_url}/v1/encounters[?since=<iso>]   -> JSON list of encounters
          GET {base_url}/v1/encounters/{id}/artifacts  -> JSON list of artifact
                                                          versions

      Auth is a ``Bearer`` token. All tests inject a ``MagicMock`` httpx client
      — no socket ever opens in CI.

PHI rules pinned in tests:

  * The connector NEVER stores a plaintext ``patient_id`` on the model — only a
    ``patient_id_hash`` (treated as already-hashed by the upstream EHR; if the
    Ambience response returns a raw ``patient_id`` the connector hashes it
    SHA-256 hex on ingest).
  * ``ScribeArtifactVersion.kind`` is a closed Literal — unknown kinds are a
    pydantic ``ValidationError``, not silently accepted.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from receipts.connectors import (
    AmbienceScribeConnector,
    ScribeArtifactVersion,
    ScribeConnector,
    ScribeEncounter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _encounter_payload(
    *,
    external_id: str = "ENC-001",
    patient_id_hash: str = "a" * 64,
    started_at: str = "2026-05-09T10:15:00Z",
    chief_complaint: str = "chest pain",
    status: str = "open",
) -> dict:
    """Encounter payload as Ambience would return it (already-hashed PHI key)."""
    return {
        "external_id": external_id,
        "patient_id_hash": patient_id_hash,
        "started_at": started_at,
        "chief_complaint": chief_complaint,
        "status": status,
    }


def _artifact_payload(
    *,
    encounter_external_id: str = "ENC-001",
    kind: str = "transcript",
    content_ref: str = "s3://ambience/enc-001/transcript-v1.txt",
    content_hash: str = "b" * 64,
    version: int = 1,
    parent_version: int | None = None,
) -> dict:
    return {
        "encounter_external_id": encounter_external_id,
        "kind": kind,
        "content_ref": content_ref,
        "content_hash": content_hash,
        "version": version,
        "parent_version": parent_version,
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
# Abstract base contract
# ---------------------------------------------------------------------------


def test_scribe_connector_is_abstract() -> None:
    """``ScribeConnector`` is an ABC — direct instantiation must fail."""
    with pytest.raises(TypeError):
        ScribeConnector()  # type: ignore[abstract]


def test_ambience_is_scribe_connector_subclass() -> None:
    """The Ambience impl declares its conformance to the interface."""
    assert issubclass(AmbienceScribeConnector, ScribeConnector)


# ---------------------------------------------------------------------------
# fetch_encounters
# ---------------------------------------------------------------------------


def test_ambience_fetch_encounters_returns_parsed_list() -> None:
    """``fetch_encounters`` decodes the encounter list into pydantic models."""
    payload = [
        _encounter_payload(external_id="ENC-001"),
        _encounter_payload(external_id="ENC-002", chief_complaint="abdominal pain"),
    ]
    client = MagicMock()
    client.get.return_value = _mock_response(status=200, json_body=payload)

    conn = AmbienceScribeConnector(api_key="amb_k3y", client=client)
    encounters = conn.fetch_encounters()

    assert [e.external_id for e in encounters] == ["ENC-001", "ENC-002"]
    assert all(isinstance(e, ScribeEncounter) for e in encounters)
    assert encounters[1].chief_complaint == "abdominal pain"

    url = client.get.call_args.args[0]
    assert url == "https://api.ambiencehealthcare.com/v1/encounters"


def test_ambience_fetch_encounters_filters_by_since() -> None:
    """``since`` must reach the API as an ISO 8601 ``since`` query param."""
    client = MagicMock()
    client.get.return_value = _mock_response(status=200, json_body=[])

    conn = AmbienceScribeConnector(api_key="amb_k3y", client=client)
    since = datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)
    conn.fetch_encounters(since=since)

    params = client.get.call_args.kwargs["params"]
    assert params["since"] == "2026-05-01T00:00:00+00:00"


def test_ambience_hashes_raw_patient_id_on_ingest() -> None:
    """If the API returns a raw ``patient_id``, the connector hashes it locally.

    This is the load-bearing PHI guard: plaintext patient IDs must never reach
    the receipts pipeline. Upstream EHRs that already hash their identifiers
    keep working unchanged via ``patient_id_hash``.
    """
    raw_pid = "MRN-12345"
    expected_hash = hashlib.sha256(raw_pid.encode("utf-8")).hexdigest()
    payload = [
        {
            "external_id": "ENC-007",
            "patient_id": raw_pid,
            "started_at": "2026-05-09T10:15:00Z",
            "chief_complaint": "shortness of breath",
            "status": "open",
        }
    ]
    client = MagicMock()
    client.get.return_value = _mock_response(status=200, json_body=payload)

    conn = AmbienceScribeConnector(api_key="amb_k3y", client=client)
    [encounter] = conn.fetch_encounters()

    assert encounter.patient_id_hash == expected_hash
    # The raw identifier must not survive on the model under any attribute.
    assert raw_pid not in encounter.model_dump_json()


# ---------------------------------------------------------------------------
# fetch_encounter_versions
# ---------------------------------------------------------------------------


def test_ambience_fetch_encounter_versions_orders_by_version() -> None:
    """Returned versions are sorted ascending so callers can rely on order."""
    payload = [
        _artifact_payload(kind="ai_note", version=3, parent_version=2),
        _artifact_payload(kind="audio", version=1, parent_version=None),
        _artifact_payload(kind="transcript", version=2, parent_version=1),
    ]
    client = MagicMock()
    client.get.return_value = _mock_response(status=200, json_body=payload)

    conn = AmbienceScribeConnector(api_key="amb_k3y", client=client)
    versions = conn.fetch_encounter_versions("ENC-001")

    assert [v.version for v in versions] == [1, 2, 3]
    assert all(isinstance(v, ScribeArtifactVersion) for v in versions)
    assert versions[0].parent_version is None
    assert versions[2].kind == "ai_note"

    url = client.get.call_args.args[0]
    assert url == "https://api.ambiencehealthcare.com/v1/encounters/ENC-001/artifacts"


def test_ambience_authorization_header_includes_api_key() -> None:
    """Every request carries ``Authorization: Bearer <api_key>``."""
    client = MagicMock()
    client.get.return_value = _mock_response(status=200, json_body=[])

    conn = AmbienceScribeConnector(api_key="amb_k3y", client=client)
    conn.fetch_encounters()
    conn.fetch_encounter_versions("ENC-001")

    for call in client.get.call_args_list:
        headers = call.kwargs["headers"]
        assert headers["Authorization"] == "Bearer amb_k3y"


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


def test_artifact_kind_validates_literal_values() -> None:
    """``kind`` is a closed Literal — unknown values are a ``ValidationError``."""
    with pytest.raises(ValidationError):
        ScribeArtifactVersion(
            encounter_external_id="ENC-001",
            kind="screenshot",  # type: ignore[arg-type]
            content_ref="s3://ambience/x.png",
            content_hash="c" * 64,
            version=1,
            parent_version=None,
        )


def test_artifact_kind_accepts_each_allowed_value() -> None:
    """All declared artifact kinds round-trip without a validation error."""
    allowed = ("audio", "transcript", "ai_note", "edited_note", "committed_note", "order")
    for kind in allowed:
        artifact = ScribeArtifactVersion(
            encounter_external_id="ENC-001",
            kind=kind,
            content_ref=f"s3://ambience/{kind}.bin",
            content_hash="d" * 64,
            version=1,
            parent_version=None,
        )
        assert artifact.kind == kind
