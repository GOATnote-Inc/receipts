"""FHIR connector (P2-3) — REST adapter over the FHIR R4 surface.

The clinical vertical reads a ``Composition`` resource (FHIR's canonical
"clinical note" envelope) and writes a receipts attestation back as a
typed ``Extension`` on the same resource. The connector is the only
network surface; every call goes through an injected ``httpx.Client`` so
tests can pin responses with a ``MagicMock`` and ``make test`` stays
hermetic.

Read path
---------
``fetch_composition`` issues ``GET {base}/Composition/{id}`` with
``Accept: application/fhir+json`` and parses the response body into a
strict pydantic ``FhirComposition``. A 404 is converted into a
``ValueError`` so callers must handle missing-resource cases explicitly
rather than silently flowing into "no drift" branches.

Write path
----------
``write_attestation_extension`` builds an ``Extension`` element matching
the ``AttestationExtension`` schema and sends it as a JSON Patch ``add``
op against the same resource:

  PATCH {base}/Composition/{id}
  Content-Type: application/json-patch+json
  Authorization: Bearer ...

The version id of the resulting resource is recovered from the server's
``Location`` response header (``Composition/{id}/_history/{ver}``); we
fall back to the response body's ``meta.versionId`` if the header is
absent.

Audit invariant
---------------
The extension URL ``https://goatnote.dev/receipts/attestation`` is a
load-bearing identifier downstream auditors grep for; it is hardcoded on
``AttestationExtension`` and asserted by the test suite.

What this module deliberately does NOT do
-----------------------------------------
- No retry / backoff: the reconciler (P2-6) owns replay semantics.
- No persistence: writing into the receipts ledger happens in P2-6.
- No PHI scrubbing: the extension payload is metadata only (model,
  prompt SHA, judge run id, Merkle hash); PHI handling lives in the
  emitter (P2-7).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field

# ----------------------------- constants -----------------------------

#: The attestation extension URL is a load-bearing audit identifier.
#: Auditors and downstream consumers grep for this exact string.
ATTESTATION_EXTENSION_URL = "https://goatnote.dev/receipts/attestation"


# ----------------------------- models -----------------------------


class FhirReference(BaseModel):
    """A FHIR ``Reference`` element (subset of the spec we actually use)."""

    reference: str
    display: str | None = None


class FhirSection(BaseModel):
    """A FHIR ``Composition.section`` (title + narrative div)."""

    title: str
    text_div: str


class FhirComposition(BaseModel):
    """Parsed view of a FHIR R4 ``Composition`` resource.

    ``type_code`` is the first coding's ``code`` (typically a LOINC code
    such as ``11488-4`` for "Consult note"). ``meta_version_id`` is
    captured at read time so optimistic-concurrency PATCH writes can
    detect server-side drift.
    """

    id: str
    status: str
    type_code: str
    subject: FhirReference
    encounter_ref: FhirReference
    date: datetime
    sections: list[FhirSection] = Field(default_factory=list)
    meta_version_id: str | None = None


class AttestationExtension(BaseModel):
    """The receipts attestation extension payload.

    Serialised onto the Composition as a FHIR ``Extension`` element whose
    nested sub-extensions carry each field. ``url`` is hardcoded so the
    audit identifier cannot be accidentally drifted.
    """

    url: str = ATTESTATION_EXTENSION_URL
    model: str
    prompt_sha: str
    judge_run_id: str
    merkle_hash: str
    recorded_at: datetime


# ----------------------------- connector -----------------------------


class FHIRConnector:
    """Thin REST client for a FHIR R4 server.

    Parameters
    ----------
    base_url:
        FHIR server base, e.g. ``https://fhir.example.com/r4``. Trailing
        slashes are stripped so URL joining stays predictable.
    bearer_token:
        OAuth / SMART-on-FHIR access token; sent as
        ``Authorization: Bearer ...``.
    client:
        Optional ``httpx.Client``. Inject a ``MagicMock`` in tests; pass
        ``None`` in production and the connector owns a new client per
        call.
    """

    def __init__(
        self,
        base_url: str,
        bearer_token: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._bearer_token = bearer_token
        self._client = client

    # ----- internals -----

    def _read_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/fhir+json",
            "Authorization": f"Bearer {self._bearer_token}",
        }

    def _patch_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/fhir+json",
            "Authorization": f"Bearer {self._bearer_token}",
            "Content-Type": "application/json-patch+json",
        }

    def _url(self, *parts: str) -> str:
        return "/".join([self._base_url, *parts])

    def _get(self, url: str) -> httpx.Response:
        if self._client is not None:
            return self._client.get(url, headers=self._read_headers())
        with httpx.Client() as client:
            return client.get(url, headers=self._read_headers())

    def _patch(self, url: str, body: list[dict[str, Any]]) -> httpx.Response:
        if self._client is not None:
            return self._client.patch(url, json=body, headers=self._patch_headers())
        with httpx.Client() as client:
            return client.patch(url, json=body, headers=self._patch_headers())

    # ----- parsing -----

    @staticmethod
    def _parse_composition(payload: dict[str, Any]) -> FhirComposition:
        type_node = payload.get("type") or {}
        codings = type_node.get("coding") or []
        type_code = ""
        if codings and isinstance(codings, list):
            first = codings[0] or {}
            type_code = str(first.get("code", "") or "")

        subject_node = payload.get("subject") or {}
        subject = FhirReference(
            reference=str(subject_node.get("reference", "") or ""),
            display=subject_node.get("display"),
        )

        encounter_node = payload.get("encounter") or {}
        encounter_ref = FhirReference(
            reference=str(encounter_node.get("reference", "") or ""),
            display=encounter_node.get("display"),
        )

        sections_raw = payload.get("section") or []
        sections: list[FhirSection] = []
        for sec in sections_raw:
            if not isinstance(sec, dict):
                continue
            text_node = sec.get("text") or {}
            sections.append(
                FhirSection(
                    title=str(sec.get("title", "") or ""),
                    text_div=str(text_node.get("div", "") or ""),
                )
            )

        meta_node = payload.get("meta") or {}
        meta_version = meta_node.get("versionId")
        meta_version_id = str(meta_version) if meta_version is not None else None

        return FhirComposition(
            id=str(payload["id"]),
            status=str(payload.get("status", "") or ""),
            type_code=type_code,
            subject=subject,
            encounter_ref=encounter_ref,
            date=payload["date"],
            sections=sections,
            meta_version_id=meta_version_id,
        )

    # ----- public API -----

    def fetch_composition(self, composition_id: str) -> FhirComposition:
        """GET ``{base}/Composition/{id}`` and parse the response body.

        Raises ``ValueError`` on 404 so callers must handle the missing
        case explicitly. Other non-2xx statuses bubble up via
        ``response.raise_for_status()``.
        """
        url = self._url("Composition", composition_id)
        response = self._get(url)
        if getattr(response, "status_code", 200) == 404:
            raise ValueError(f"FHIR Composition not found: {composition_id}")
        response.raise_for_status()
        payload = response.json()
        return self._parse_composition(payload)

    @staticmethod
    def _build_extension_value(payload: dict[str, Any]) -> dict[str, Any]:
        """Materialise the receipts extension element from a metadata dict."""
        ext = AttestationExtension(
            model=str(payload["model"]),
            prompt_sha=str(payload["prompt_sha"]),
            judge_run_id=str(payload["judge_run_id"]),
            merkle_hash=str(payload["merkle_hash"]),
            recorded_at=payload.get("recorded_at") or datetime.now(UTC),
        )
        return {
            "url": ext.url,
            "extension": [
                {"url": "model", "valueString": ext.model},
                {"url": "prompt_sha", "valueString": ext.prompt_sha},
                {"url": "judge_run_id", "valueString": ext.judge_run_id},
                {"url": "merkle_hash", "valueString": ext.merkle_hash},
                {"url": "recorded_at", "valueDateTime": ext.recorded_at.isoformat()},
            ],
        }

    @staticmethod
    def _version_id_from_location(location: str | None) -> str | None:
        """Pull the version id out of a FHIR ``Location`` header.

        FHIR servers return ``[base/]ResourceType/{id}/_history/{ver}``.
        We don't anchor on ``base`` because servers vary; we just split
        on ``_history`` and take the trailing segment.
        """
        if not location:
            return None
        if "_history/" not in location:
            return None
        tail = location.split("_history/", 1)[1]
        # Trim trailing slash or query string just in case.
        tail = tail.split("?", 1)[0].rstrip("/")
        return tail or None

    def write_attestation_extension(
        self,
        composition_id: str,
        attestation_payload: dict[str, Any],
    ) -> str:
        """Patch a Composition with the receipts attestation extension.

        Returns the new resource version id, sourced from the server's
        ``Location`` response header (or ``meta.versionId`` of the
        returned body as a fallback).
        """
        url = self._url("Composition", composition_id)
        ext_value = self._build_extension_value(attestation_payload)
        patch_body: list[dict[str, Any]] = [
            {
                "op": "add",
                "path": "/extension/-",
                "value": ext_value,
            }
        ]
        response = self._patch(url, patch_body)
        response.raise_for_status()

        location = None
        headers = getattr(response, "headers", None)
        if headers is not None:
            # httpx headers are case-insensitive; MagicMock dicts are not.
            try:
                location = headers.get("Location") or headers.get("location")
            except AttributeError:
                location = None

        version_id = self._version_id_from_location(location)
        if version_id:
            return version_id

        # Fallback: read meta.versionId from the body the server returns.
        try:
            body = response.json()
        except Exception:  # pragma: no cover - defensive parse guard
            body = {}
        meta = (body or {}).get("meta") or {}
        version = meta.get("versionId")
        if version is None:
            raise RuntimeError(
                "FHIR server returned no Location header and no meta.versionId; "
                f"cannot determine new version of Composition/{composition_id}"
            )
        return str(version)
