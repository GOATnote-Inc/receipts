"""L2: Merkle hash chain over the `attestation` table.

Chain rules (RFC-style):
- Hash is SHA-256 of `canonical_json(payload) + prev_hash`.
- Canonical JSON: `json.dumps(payload, sort_keys=True, separators=(",", ":"))`.
- Genesis row's prev_hash is the empty string "".
- Each subsequent row's prev_hash is the immediately preceding row's hash
  ordered by id ASC.

`MerkleLog` is the thin operational facade; `compute_hash` is exposed for
callers that need to derive a hash without hitting the database (e.g. a
detached verifier).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from receipts.ledger.models import Attestation


def _canonical_json(payload: Any) -> str:
    """Stable JSON serialization for hash input."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def compute_hash(payload: dict, prev_hash: str) -> str:
    """SHA-256 of `canonical_json(payload) + prev_hash`."""
    blob = (_canonical_json(payload) + prev_hash).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class MerkleLog:
    """Append-only Merkle-chained log backed by the `attestation` table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _last_hash(self) -> str:
        """Hash of the most recent (by id DESC) row, or "" if none yet."""
        last = self._session.query(Attestation).order_by(Attestation.id.desc()).first()
        if last is None:
            return ""
        return last.hash

    def append(
        self,
        payload: dict,
        *,
        kind: str,
        target_id: int,
        target_kind: str,
    ) -> str:
        """Append a row to the chain. Returns the new row's hash."""
        prev_hash = self._last_hash()
        new_hash = compute_hash(payload, prev_hash)
        row = Attestation(
            kind=kind,
            target_id=target_id,
            target_kind=target_kind,
            hash=new_hash,
            prev_hash=prev_hash,
            payload=payload,
        )
        self._session.add(row)
        self._session.commit()
        return new_hash

    def verify_chain(self) -> list[int]:
        """Walk the chain and return ids of rows whose hash or linkage is wrong.

        Empty list means the chain is intact. A row is considered bad if:
        - its stored prev_hash != preceding row's hash, OR
        - its stored hash != compute_hash(payload, stored prev_hash).
        """
        bad: list[int] = []
        expected_prev = ""
        rows = self._session.query(Attestation).order_by(Attestation.id.asc()).all()
        for row in rows:
            stored_prev = row.prev_hash or ""
            if stored_prev != expected_prev:
                bad.append(row.id)
                # Still re-anchor against what's recorded so we don't
                # cascade-flag every subsequent row purely on a single break.
                expected_prev = row.hash
                continue
            if compute_hash(row.payload, stored_prev) != row.hash:
                bad.append(row.id)
            expected_prev = row.hash
        return bad

    def tamper_detect(self) -> list[int]:
        """Return ids of rows whose hash != compute_hash(payload, prev_hash)."""
        bad: list[int] = []
        rows = self._session.query(Attestation).order_by(Attestation.id.asc()).all()
        for row in rows:
            stored_prev = row.prev_hash or ""
            if compute_hash(row.payload, stored_prev) != row.hash:
                bad.append(row.id)
        return bad


__all__ = ["MerkleLog", "compute_hash"]
