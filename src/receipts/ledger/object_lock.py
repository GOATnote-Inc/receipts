"""L5: S3 Object Lock-backed artifact store.

Wraps a boto3 S3 client with COMPLIANCE-mode Object Lock retention so
attestation artifacts (Merkle roots, judge rationales, exported ledger
snapshots) cannot be silently overwritten or deleted inside the retention
window — not even by the AWS account root.

Retention defaults to 6 years (regulatory floor for clinical
attestations). Callers needing long-horizon record-keeping pass
`retention_years=25`.

The store stamps the artifact's SHA-256 into S3 object metadata so a
detached verifier can re-download and prove byte-for-byte equivalence
without trusting any intermediary cache.

`dateutil` is intentionally avoided; `timedelta(days=365 * years)` is
sufficient — the few-hour drift across leap years is far inside the
ObjectLockRetainUntilDate granularity.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any


class ObjectLockStore:
    """Append-only artifact store backed by S3 Object Lock (COMPLIANCE mode)."""

    def __init__(
        self,
        bucket: str,
        client: Any = None,
        retention_years: int = 6,
    ) -> None:
        if retention_years <= 0:
            raise ValueError("retention_years must be positive")
        self._bucket = bucket
        self._client = client
        self.retention_years = retention_years

    @property
    def bucket(self) -> str:
        return self._bucket

    @property
    def client(self) -> Any:
        """Lazily construct a boto3 S3 client when one wasn't injected."""
        if self._client is None:
            import boto3  # local import — keep boto3 a soft dep at import time

            self._client = boto3.client("s3")
        return self._client

    def retention_until(self, years: int | None = None) -> datetime:
        """Compute the RetainUntilDate for an artifact written `now`."""
        span = years if years is not None else self.retention_years
        if span <= 0:
            raise ValueError("years must be positive")
        return datetime.now(UTC) + timedelta(days=365 * span)

    def put_artifact(self, key: str, body: bytes, content_hash: str) -> dict:
        """Write `body` to S3 under `key` with COMPLIANCE-mode retention.

        Returns the raw boto3 `put_object` response dict.
        """
        retain_until = self.retention_until()
        return self.client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=retain_until,
            Metadata={"sha256": content_hash},
        )

    def get_artifact(self, key: str) -> bytes:
        """Read an artifact's bytes back from S3."""
        resp = self.client.get_object(Bucket=self._bucket, Key=key)
        return resp["Body"].read()

    def verify_hash(self, key: str, expected_hash: str) -> bool:
        """Re-download `key`, compute its SHA-256, compare to `expected_hash`."""
        body = self.get_artifact(key)
        return hashlib.sha256(body).hexdigest() == expected_hash


__all__ = ["ObjectLockStore"]
