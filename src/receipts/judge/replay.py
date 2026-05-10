"""Judge record/replay store (J7).

The L2 LLM layer is the only non-deterministic step in the CEIS judge stack.
This module provides a hermetic record/replay shim so the test suite -- and
any reproducibility audit -- can pin judge calls to fixture files keyed by
a stable hash over the call inputs.

Why this exists
---------------
- ``make test`` must never make a live judge call. Default mode is ``replay``.
- Cross-process determinism: a recording captured locally must be byte-for-byte
  reproducible in CI. We achieve this with canonical JSON encoding
  (``sort_keys=True, separators=(",", ":")``) fed to sha256.
- Stdlib only -- no pydantic, no third-party serialisers. ``JudgeCall`` and
  ``JudgeRecording`` are plain ``@dataclass`` so the hash is computed over
  the fields we control, not over Pydantic's BaseModel internals.

Storage layout
--------------
Recordings live at ``fixtures/judge_recordings/{stable_hash}.json``. Each file
is a JSON document containing the full ``JudgeCall`` plus the recorded
``response`` / ``latency_ms`` / ``cost_usd`` / ``recorded_at``. The filename
*is* the lookup key -- locating a recording is ``stable_hash(call)`` plus a
filesystem read, no index.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class JudgeCall:
    """Inputs that fully determine a judge invocation.

    Every field participates in :func:`stable_hash`. ``temperature`` is a
    ``float`` (not ``Decimal``); JSON round-trips ``0.0`` and ``0.7`` exactly
    so equality across processes is preserved.
    """

    model: str
    prompt: str
    temperature: float
    seed: int | None
    input_payload: dict


@dataclass(frozen=True)
class JudgeRecording:
    """A captured judge call plus the response it produced.

    ``recorded_at`` is a UTC ISO8601 timestamp (always tz-aware) so downstream
    tooling can audit when each fixture was captured without parsing
    ambiguous local times.
    """

    call: JudgeCall
    response: dict
    latency_ms: int
    cost_usd: float
    recorded_at: str


def _canonical_call_json(call: JudgeCall) -> str:
    """Canonical JSON encoding of a ``JudgeCall``.

    ``sort_keys=True`` + the tightest separators give a single byte sequence
    per logical call regardless of dict insertion order or local indentation
    settings. This is the precondition for cross-process hash stability.
    """
    return json.dumps(asdict(call), sort_keys=True, separators=(",", ":"))


def stable_hash(call: JudgeCall) -> str:
    """Return the sha256 hex digest of the canonical JSON of ``call``.

    Hash inputs: model, prompt, temperature, seed, input_payload. Two
    ``JudgeCall`` instances that differ only in dict-key insertion order
    collide; any other difference (including ``temperature=0.0`` vs ``0.7``,
    ``seed=None`` vs ``seed=0``) yields a distinct hash.
    """
    payload = _canonical_call_json(call).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class ReplayStore:
    """Filesystem-backed record/replay store for judge calls.

    The store is intentionally minimal: one JSON file per recording, named
    after the hash of the call. No index, no metadata DB, no locking -- a
    record-mode run is just ``json.dump`` and a replay-mode run is just
    ``json.load``. This keeps fixtures auditable by humans and trivial to
    commit / diff.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # ------------------------------ record ------------------------------

    def record(
        self,
        call: JudgeCall,
        *,
        response: dict,
        latency_ms: int,
        cost_usd: float,
    ) -> None:
        """Persist a recording to ``{path}/{stable_hash}.json``.

        ``path`` is created on demand so callers can point the store at a
        ``tmp_path`` (tests) or at ``fixtures/judge_recordings/`` (real
        captures) without pre-creating directories.
        """
        self.path.mkdir(parents=True, exist_ok=True)
        recording = JudgeRecording(
            call=call,
            response=response,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            recorded_at=datetime.now(UTC).isoformat(),
        )
        target = self.path / f"{stable_hash(call)}.json"
        # ``sort_keys=True`` keeps committed fixtures stable across machines;
        # ``indent=2`` keeps them human-diffable in PR review.
        target.write_text(
            json.dumps(asdict(recording), sort_keys=True, indent=2),
            encoding="utf-8",
        )

    # ------------------------------ replay ------------------------------

    def replay(self, call: JudgeCall) -> JudgeRecording:
        """Load the recording for ``call`` or raise ``FileNotFoundError``.

        Surfacing ``FileNotFoundError`` (rather than returning ``None``) lets
        the CEIS judge driver treat "no recording" as a hard test failure --
        a missing fixture in replay-mode means the trajectory drifted and
        somebody needs to re-record, not silently fall back to a live call.
        """
        target = self.path / f"{stable_hash(call)}.json"
        raw = json.loads(target.read_text(encoding="utf-8"))
        call_raw = raw["call"]
        return JudgeRecording(
            call=JudgeCall(
                model=call_raw["model"],
                prompt=call_raw["prompt"],
                temperature=call_raw["temperature"],
                seed=call_raw["seed"],
                input_payload=call_raw["input_payload"],
            ),
            response=raw["response"],
            latency_ms=raw["latency_ms"],
            cost_usd=raw["cost_usd"],
            recorded_at=raw["recorded_at"],
        )

    # ------------------------------ mode ------------------------------

    @staticmethod
    def mode_from_env() -> Literal["record", "replay"]:
        """Return the active mode, defaulting to ``"replay"``.

        Reads ``RECEIPTS_JUDGE_MODE``. Unknown values fall back to ``"replay"``
        -- "fail closed" against live API calls is the safer default for a
        repo where the stop-hook gates on judge κ.
        """
        mode = os.environ.get("RECEIPTS_JUDGE_MODE", "replay")
        if mode == "record":
            return "record"
        return "replay"
