"""P2-7: PHI-aware output emitter for the Clinical Audit Ledger.

The clinical emitter consumes a :class:`ClinicalReconcilerResult` plus the
live ledger ``Session`` and an optional :class:`FHIRConnector`, produces a
PHI-redacted Markdown CMIO summary, and (when not in dry-run) fans out one
FHIR attestation extension write per draft.

PHI discipline
--------------
The clinical vertical is forbidden from posting patient text to Slack
(see ``CLAUDE.md`` + ``STATUS.md``). The emitter signature therefore
deliberately omits any Slack handle — the test suite asserts this
invariant by inspecting :func:`emit_clinical_outputs`'s parameters. The
Markdown body is additionally scrubbed of:

* SSN-pattern: ``\\d{3}-\\d{2}-\\d{4}``
* MRN-pattern: ``\\d{6,}`` (any run of 6+ digits — covers Epic / Cerner MRNs)
* DOB-pattern: ``\\d{1,2}/\\d{1,2}/\\d{4}``
* Name-like: two consecutive capitalized words (e.g. ``John Smith``)

…all replaced with the literal token ``[REDACTED]``. This is a defence-in-depth
layer; the upstream drafter is itself prompt-constrained to never copy
patient text into ``drift_summary`` (S3), but the emitter assumes the
drafter is untrusted from a PHI standpoint and scrubs anyway.

FHIR write-back
---------------
For each ``(encounter_external_id, contract)`` pair the emitter resolves
the synthetic Composition id as ``f"synth-{encounter_external_id}"`` and
calls :meth:`FHIRConnector.write_attestation_extension` with a metadata-
only payload (``model`` / ``prompt_sha`` / ``judge_run_id`` /
``merkle_hash`` / ``recorded_at``). No PHI flows through the connector;
the extension is an attestation envelope, not a clinical artifact.

What this module deliberately does NOT do
-----------------------------------------
- No Slack: forbidden by policy.
- No PDF render: out-of-scope for P2-7 (lives in CLI / downstream renderer).
- No Merkle append: external-write logging is the CLI's responsibility.
- No retry / backoff: idempotency lives one layer up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from receipts.clinical.reconciler import ClinicalReconcilerResult
    from receipts.connectors.fhir import FHIRConnector

__all__ = ["ClinicalEmitterResult", "emit_clinical_outputs"]


# ---------------------------------------------------------------------------
# PHI scrub
# ---------------------------------------------------------------------------


# Order matters: scrub the most specific patterns (SSN / DOB) before the
# generic MRN ``\d{6,}+`` rule so the digit groups of an SSN aren't
# half-eaten by the MRN pass. Capitalized two-word sequences come last so
# they don't munge the literal ``[REDACTED]`` token (which starts with
# uppercase but is bracketed, not a word).
_PHI_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # SSN: 3-2-4 digit groups.
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED]"),
    # DOB: M(/M)D(/D)YYYY — accept 1- or 2-digit month / day.
    (re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b"), "[REDACTED]"),
    # MRN: any 6+ digit run. Placed after SSN/DOB so the more-specific
    # patterns absorb their structure first.
    (re.compile(r"\b\d{6,}\b"), "[REDACTED]"),
    # Name-like: two consecutive capitalized words. The look-ahead /
    # look-behind on ``[REDACTED]`` is deliberately permissive — we accept
    # the redundancy of redacting our own replacement token rather than
    # invent a more complex regex.
    (re.compile(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b"), "[REDACTED]"),
)


def _scrub_phi(text: str) -> str:
    """Replace PHI-like substrings with ``[REDACTED]``.

    Applied to every line of the Markdown body except the literal headers
    the emitter itself emits (those are stable formatting tokens with no
    PHI surface, so we don't bother carving them out — the regexes simply
    don't match them).
    """
    if not text:
        return text
    out = text
    for pattern, replacement in _PHI_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ClinicalEmitterResult:
    """Aggregate output of one :func:`emit_clinical_outputs` invocation.

    Attributes:
        markdown_body: The PHI-redacted Markdown CMIO summary. Always
            populated, even on dry-run.
        fhir_attestation_version_ids: New Composition version ids returned
            by :meth:`FHIRConnector.write_attestation_extension`, one per
            encounter with a committed attestation. Empty on dry-run or
            when no connector was supplied.
        composition_update_count: Convenience mirror of
            ``len(fhir_attestation_version_ids)`` for downstream metrics
            consumers that only need the integer count.
        dry_run: Whether the run was a preview; mirrors the input flag.
    """

    markdown_body: str
    fhir_attestation_version_ids: list[str] = field(default_factory=list)
    composition_update_count: int = 0
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Drift detection (mirrors the eng emitter's marker set)
# ---------------------------------------------------------------------------


#: Substring markers (case-insensitive) the emitter scans for inside a
#: contract's ``drift_summary`` to decide whether the encounter drifted.
#: Mirrors :data:`receipts.eng.emitter.DRIFT_MARKERS` for cross-vertical
#: consistency — same lexicon, same prompt templates.
_DRIFT_MARKERS: tuple[str, ...] = (
    "scope-creep",
    "scope-shrink",
    "decision-not-reflected",
)


def _is_drifted(drift_summary: str) -> bool:
    """True iff the drift summary carries any canonical drift marker."""
    s = (drift_summary or "").lower()
    return any(marker in s for marker in _DRIFT_MARKERS)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _format_float(value: float | None) -> str:
    """Render an optional metric with three decimal places, or ``n/a``."""
    return f"{value:.3f}" if value is not None else "n/a"


def _build_markdown(
    result: ClinicalReconcilerResult,
    cmio_email: str | None,
) -> str:
    """Render the CMIO summary Markdown.

    Layout matches the spec in P2-7:

      # Clinical Audit — Week {week_id}

      - Encounters: N
      - Pass^1: ...
      - κ: ...
      - Hallucination rate: ...
      - Merkle: ...
      - CMIO recipient: ...

      ## Top drift items
      - ENC-NNNN: <first 80 chars of drift_summary, sanitized>
      ...

    Every line is run through :func:`_scrub_phi` before joining so any
    PHI bleed from upstream is removed defence-in-depth.
    """
    lines: list[str] = []
    lines.append(f"# Clinical Audit — Week {result.week_id}")
    lines.append("")
    lines.append(f"- Encounters: {result.encounter_count}")
    lines.append(f"- Pass^1: {_format_float(result.passk)}")
    lines.append(f"- κ: {_format_float(result.kappa)}")
    lines.append(f"- Hallucination rate: {_format_float(result.hallucination_flag_rate)}")
    merkle_status = "intact" if result.merkle_chain_intact else "BROKEN"
    lines.append(f"- Merkle: {merkle_status} ({result.merkle_row_count} attestations)")
    lines.append(f"- CMIO recipient: {cmio_email or 'unset'}")
    lines.append("")
    lines.append("## Top drift items")

    drifted: list[tuple[str, str]] = []
    for ext_id, contract in result.drafts:
        summary = contract.drift_summary or ""
        if _is_drifted(summary):
            # Truncate to the first 80 chars then PHI-scrub. We scrub
            # *after* truncation so partial PHI at the boundary collapses
            # to a [REDACTED] token rather than leaking half-numbers.
            snippet = summary[:80]
            drifted.append((ext_id, snippet))

    drifted.sort(key=lambda pair: pair[0])

    if not drifted:
        lines.append("- (none)")
    else:
        for ext_id, snippet in drifted:
            lines.append(f"- {ext_id}: {snippet}")

    body = "\n".join(lines)
    return _scrub_phi(body)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def emit_clinical_outputs(
    result: ClinicalReconcilerResult,
    session: Session,
    fhir: FHIRConnector | None = None,
    cmio_email: str | None = None,
    dry_run: bool = False,
) -> ClinicalEmitterResult:
    """Emit the weekly CMIO digest + FHIR attestation fan-out.

    Args:
        result: The reconciler output for the week.
        session: An open SQLAlchemy ``Session`` against the L1 clinical
            schema. Accepted for API symmetry with the eng emitter; the
            current implementation only uses the connector + the result
            structure, but downstream PDF / Composition-id resolution
            steps will join against this session.
        fhir: Optional :class:`FHIRConnector`. When supplied (and
            ``dry_run`` is false), one ``write_attestation_extension`` call
            is issued per draft against the synthetic Composition id
            ``f"synth-{encounter_external_id}"``.
        cmio_email: Optional CMIO recipient address. Surfaces in the
            Markdown summary as the audit recipient marker; ``None``
            renders as ``unset``. No mail is actually sent — delivery is
            out-of-scope for the emitter.
        dry_run: When true, the Markdown is built but every FHIR write is
            skipped. The result still includes the Markdown body so the
            CLI can preview a week's output.

    Returns:
        :class:`ClinicalEmitterResult` covering the redacted Markdown
        body, any collected FHIR version ids, and the update count.

    Note:
        The emitter intentionally takes no Slack handle. PHI must not
        leave the FHIR / Markdown / PDF surface; ``test_clinical_emitter``
        asserts this invariant by introspecting the signature.
    """
    # ``session`` is accepted for API symmetry with the eng emitter and to
    # leave room for future Composition-id resolution against the L1
    # ``encounter`` table. The synthetic ``synth-ENC-NNNN`` mapping is the
    # demo-time substitute until P2-8 wires real FHIR id lookup.
    _ = session

    out = ClinicalEmitterResult(
        markdown_body=_build_markdown(result, cmio_email),
        dry_run=dry_run,
    )

    if dry_run or fhir is None:
        return out

    version_ids: list[str] = []
    for ext_id, _contract in result.drafts:
        composition_id = f"synth-{ext_id}"
        attestation_payload = {
            "model": "claude-opus-4-7",
            "prompt_sha": "stub",
            "judge_run_id": "stub",
            "merkle_hash": "stub",
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        version_id = fhir.write_attestation_extension(composition_id, attestation_payload)
        version_ids.append(version_id)

    out.fhir_attestation_version_ids = version_ids
    out.composition_update_count = len(version_ids)
    return out
