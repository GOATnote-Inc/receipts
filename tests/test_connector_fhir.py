"""Tests for the FHIR connector (P2-3).

The FHIR connector is the clinical-side counterpart to the engineering
connectors: it reads a ``Composition`` resource (the canonical "clinical
note" envelope in FHIR R4) and writes a receipts attestation back as a
typed ``Extension`` on that same resource.

Hermeticity contract
--------------------
- Every test injects a ``MagicMock`` ``httpx.Client``; no real FHIR server
  is contacted from the suite.
- Response payloads are hand-crafted to match the FHIR R4 wire shape so
  schema drift in the parser is caught by these tests.
- The attestation extension URL is a load-bearing invariant (it is what
  downstream auditors grep for); the URL is asserted explicitly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from receipts.connectors import (
    AttestationExtension,
    FhirComposition,
    FHIRConnector,
)

# --------------------------- helpers ---------------------------


def _composition_payload(
    composition_id: str = "comp-1",
    status: str = "final",
    version_id: str = "3",
) -> dict:
    """Return a realistic FHIR R4 Composition JSON resource."""
    return {
        "resourceType": "Composition",
        "id": composition_id,
        "meta": {
            "versionId": version_id,
            "lastUpdated": "2026-05-09T12:00:00Z",
        },
        "status": status,
        "type": {
            "coding": [
                {
                    "system": "http://loinc.org",
                    "code": "11488-4",
                    "display": "Consult note",
                }
            ],
        },
        "subject": {
            "reference": "Patient/abc-123",
            "display": "Jane Doe",
        },
        "encounter": {
            "reference": "Encounter/enc-99",
        },
        "date": "2026-05-09T11:30:00Z",
        "section": [
            {
                "title": "Chief Complaint",
                "text": {
                    "status": "generated",
                    "div": "<div>Chest pain x 2 hours</div>",
                },
            },
            {
                "title": "Assessment",
                "text": {
                    "status": "generated",
                    "div": "<div>Rule out ACS</div>",
                },
            },
        ],
    }


def _mock_response(
    payload: dict | None = None,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """Build a MagicMock httpx.Response with the supplied JSON and headers."""
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload or {}
    response.headers = headers or {}
    response.raise_for_status.return_value = None
    return response


def _mock_client(response: MagicMock) -> MagicMock:
    """Build a MagicMock httpx.Client whose verb methods return ``response``."""
    client = MagicMock()
    client.get.return_value = response
    client.patch.return_value = response
    client.put.return_value = response
    client.post.return_value = response
    return client


# --------------------------- fetch_composition ---------------------------


def test_fetch_composition_returns_parsed_resource() -> None:
    payload = _composition_payload()
    client = _mock_client(_mock_response(payload))
    conn = FHIRConnector(
        base_url="https://fhir.example.com/r4",
        bearer_token="tok-test",
        client=client,
    )

    comp = conn.fetch_composition("comp-1")

    assert isinstance(comp, FhirComposition)
    assert comp.id == "comp-1"
    assert comp.status == "final"
    assert comp.type_code == "11488-4"
    assert comp.subject.reference == "Patient/abc-123"
    assert comp.subject.display == "Jane Doe"
    assert comp.encounter_ref.reference == "Encounter/enc-99"
    assert isinstance(comp.date, datetime)
    assert comp.date.tzinfo is not None
    assert len(comp.sections) == 2
    assert comp.sections[0].title == "Chief Complaint"
    assert comp.sections[0].text_div == "<div>Chest pain x 2 hours</div>"
    assert comp.sections[1].title == "Assessment"
    assert comp.meta_version_id == "3"

    # GET went to {base}/Composition/{id} with FHIR Accept header + bearer.
    args, kwargs = client.get.call_args
    assert args[0] == "https://fhir.example.com/r4/Composition/comp-1"
    headers = kwargs.get("headers", {})
    assert headers.get("Accept") == "application/fhir+json"
    assert headers.get("Authorization") == "Bearer tok-test"


def test_fetch_composition_handles_404() -> None:
    """A 404 from the FHIR server raises ValueError (documented contract).

    We pick ValueError (not None) so downstream code is forced to handle
    missing-resource cases explicitly -- silently returning None would
    smear into the reconciler's "no drift" branch.
    """
    response = _mock_response({"resourceType": "OperationOutcome"}, status_code=404)
    client = _mock_client(response)
    conn = FHIRConnector(
        base_url="https://fhir.example.com/r4",
        bearer_token="tok-test",
        client=client,
    )

    with pytest.raises(ValueError):
        conn.fetch_composition("does-not-exist")


# --------------------------- write_attestation_extension ---------------------------


def test_write_attestation_patch_payload_shape() -> None:
    response = _mock_response(
        _composition_payload(version_id="4"),
        status_code=200,
        headers={"Location": "Composition/comp-1/_history/4"},
    )
    client = _mock_client(response)
    conn = FHIRConnector(
        base_url="https://fhir.example.com/r4",
        bearer_token="tok-test",
        client=client,
    )

    payload = {
        "model": "claude-opus-4-7",
        "prompt_sha": "sha256:abc",
        "judge_run_id": "run-42",
        "merkle_hash": "0xdeadbeef",
    }
    conn.write_attestation_extension("comp-1", payload)

    # PATCH was the verb used.
    assert client.patch.called, "expected a PATCH request"
    args, kwargs = client.patch.call_args
    assert args[0] == "https://fhir.example.com/r4/Composition/comp-1"

    body = kwargs.get("json")
    assert body is not None, "PATCH must carry a JSON body"

    # Body is a JSON Patch list whose op adds the receipts extension.
    assert isinstance(body, list)
    add_ops = [op for op in body if op.get("op") == "add"]
    assert add_ops, "PATCH body must include at least one 'add' op"
    add_op = add_ops[0]
    assert add_op["path"].startswith("/extension")
    ext = add_op["value"]

    # Extension element matches the AttestationExtension schema.
    assert ext["url"] == "https://goatnote.dev/receipts/attestation"
    nested = {sub["url"]: sub for sub in ext.get("extension", [])}
    assert nested["model"]["valueString"] == "claude-opus-4-7"
    assert nested["prompt_sha"]["valueString"] == "sha256:abc"
    assert nested["judge_run_id"]["valueString"] == "run-42"
    assert nested["merkle_hash"]["valueString"] == "0xdeadbeef"
    assert "recorded_at" in nested
    # recorded_at is ISO-8601 with offset.
    assert "T" in nested["recorded_at"]["valueDateTime"]


def test_write_attestation_returns_version_id_from_location_header() -> None:
    response = _mock_response(
        _composition_payload(version_id="7"),
        status_code=200,
        headers={"Location": "https://fhir.example.com/r4/Composition/comp-1/_history/7"},
    )
    client = _mock_client(response)
    conn = FHIRConnector(
        base_url="https://fhir.example.com/r4",
        bearer_token="tok-test",
        client=client,
    )

    version_id = conn.write_attestation_extension(
        "comp-1",
        {
            "model": "claude-opus-4-7",
            "prompt_sha": "sha256:abc",
            "judge_run_id": "run-1",
            "merkle_hash": "0xfeedface",
        },
    )

    assert version_id == "7"


def test_write_attestation_auth_header_present() -> None:
    response = _mock_response(
        _composition_payload(version_id="2"),
        status_code=200,
        headers={"Location": "Composition/comp-1/_history/2"},
    )
    client = _mock_client(response)
    conn = FHIRConnector(
        base_url="https://fhir.example.com/r4",
        bearer_token="tok-test",
        client=client,
    )

    conn.write_attestation_extension(
        "comp-1",
        {
            "model": "claude-opus-4-7",
            "prompt_sha": "sha256:abc",
            "judge_run_id": "run-1",
            "merkle_hash": "0xfeedface",
        },
    )

    _, kwargs = client.patch.call_args
    headers = kwargs.get("headers", {})
    assert headers.get("Authorization") == "Bearer tok-test"
    # FHIR R4 JSON Patch must use the json-patch+json Content-Type.
    assert headers.get("Content-Type") == "application/json-patch+json"


def test_attestation_extension_url_invariant() -> None:
    """The extension URL is a load-bearing audit identifier; pin it explicitly."""
    ext = AttestationExtension(
        model="claude-opus-4-7",
        prompt_sha="sha256:abc",
        judge_run_id="run-1",
        merkle_hash="0xdeadbeef",
        recorded_at=datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC),
    )
    assert ext.url == "https://goatnote.dev/receipts/attestation"
