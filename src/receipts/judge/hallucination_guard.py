"""J6: judge-hallucination guard.

The L2 LLM judge frequently cites supporting text from a source ledger
entry. The V9 overlay analysis on healthcraft showed that 73% of
judge-vs-human disagreements were *judge hallucinations* -- the judge cited
content that did not literally appear in the source. The guard checks each
cited substring against the source it was attributed to and flags
hallucinations before they propagate into the run log.

Two-step check
--------------
1. **Literal substring** -- normalise both citation and source (lowercase,
   collapse internal whitespace, strip leading/trailing punctuation) and
   look for the normalised citation inside the normalised source. A literal
   hit is the strongest possible signal; the guard returns ``None``.
2. **Token-Jaccard fallback** -- when the literal check misses, compute the
   token-Jaccard similarity between citation and source. Punctuation is
   stripped before tokenisation. A score ``>= similarity_threshold`` is
   treated as a tolerable paraphrase and returns ``None``.

Outcomes
--------
- ``None``                            -> no hallucination detected
- ``HallucinationFlag(kind="literal_missing", similarity_score=0.0)``
    -- zero token overlap (no shared words) and no literal hit
- ``HallucinationFlag(kind="low_similarity", similarity_score=jaccard)``
    -- some overlap but below threshold

Design choices
--------------
- Stdlib-only (``string``, ``re``, ``dataclasses``). The guard sits inside
  the dual-judge hot path on every L2 invocation; we avoid third-party
  tokenisation dependencies.
- ``HallucinationFlag`` is a frozen-by-convention ``@dataclass`` rather than
  a Pydantic model. The shape is closed and instantiation cost matters.
- ``check_batch`` returns ``(flags, flag_rate)`` so the κ stop-hook can pin
  flag_rate as a release-gate metric without re-scanning every record.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from typing import Literal

__all__ = ["HallucinationFlag", "HallucinationGuard"]

FlagKind = Literal["literal_missing", "low_similarity"]

# Characters stripped from the leading/trailing edges of an input string
# during normalisation. We strip the same set from token edges before the
# Jaccard step so that ``"cough"`` and ``"cough,"`` collide.
_EDGE_PUNCT = ".,;:!?\"'()"

# Pre-compiled whitespace collapser. ``re.sub`` with a compiled pattern is
# materially faster on the hot path than ``" ".join(s.split())`` when the
# guard sweeps a batch of citations.
_WHITESPACE_RE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    """Lowercase, collapse whitespace, and strip outer punctuation.

    Used identically on both the citation and the source so the substring
    check is symmetric with respect to formatting noise.
    """

    collapsed = _WHITESPACE_RE.sub(" ", text).strip().lower()
    return collapsed.strip(_EDGE_PUNCT + string.whitespace)


def _tokenise(text: str) -> set[str]:
    """Token set used for Jaccard similarity.

    Splits on whitespace, lowercases, then strips the edge-punctuation set
    from each token. Empty tokens (which can appear if a token was nothing
    but punctuation) are dropped.
    """

    tokens: set[str] = set()
    for raw in text.lower().split():
        cleaned = raw.strip(_EDGE_PUNCT)
        if cleaned:
            tokens.add(cleaned)
    return tokens


def _jaccard(a: set[str], b: set[str]) -> float:
    """Token-set Jaccard similarity ``|A ∩ B| / |A ∪ B|``.

    Returns ``0.0`` when both sides are empty, matching the contract
    declared in the J6 spec (empty union -> 0.0).
    """

    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


@dataclass(frozen=True)
class HallucinationFlag:
    """Single hallucination finding emitted by :class:`HallucinationGuard`.

    Attributes
    ----------
    citation_text:
        The exact citation text the judge claimed appeared in ``source_id``.
        Preserved verbatim (not normalised) so downstream audit logs can
        round-trip the original judge output.
    source_id:
        Identifier of the source the judge attributed the citation to.
    kind:
        ``"literal_missing"`` for zero-overlap hallucinations,
        ``"low_similarity"`` when some tokens overlap but the Jaccard score
        is below ``similarity_threshold``.
    similarity_score:
        Computed token-Jaccard score in ``[0.0, 1.0]``. ``0.0`` for
        ``literal_missing`` flags.
    """

    citation_text: str
    source_id: str
    kind: FlagKind
    similarity_score: float


class HallucinationGuard:
    """Two-step literal + Jaccard guard against judge-citation hallucination.

    Parameters
    ----------
    similarity_threshold:
        Minimum token-Jaccard score required to *tolerate* a citation that
        failed the literal substring check. Defaults to ``0.40``, chosen
        from the healthcraft V9 overlay calibration (paraphrases of clinical
        content rarely fell below this; outright hallucinations very rarely
        sat above it).
    """

    def __init__(self, similarity_threshold: float = 0.40) -> None:
        self.similarity_threshold = similarity_threshold

    def check_citation(
        self,
        citation_text: str,
        source_text: str,
        source_id: str,
    ) -> HallucinationFlag | None:
        """Check a single ``(citation, source)`` pair.

        Returns ``None`` when the citation is grounded -- either by literal
        substring match or by a paraphrase whose Jaccard meets the
        threshold. Otherwise returns the appropriate :class:`HallucinationFlag`.
        """

        normalised_citation = _normalise(citation_text)
        normalised_source = _normalise(source_text)

        # Step 1: literal substring (after normalisation).
        if normalised_citation and normalised_citation in normalised_source:
            return None

        # Step 2: token-Jaccard similarity fallback.
        citation_tokens = _tokenise(citation_text)
        source_tokens = _tokenise(source_text)
        similarity = _jaccard(citation_tokens, source_tokens)

        if similarity >= self.similarity_threshold:
            return None

        if similarity == 0.0:
            return HallucinationFlag(
                citation_text=citation_text,
                source_id=source_id,
                kind="literal_missing",
                similarity_score=0.0,
            )
        return HallucinationFlag(
            citation_text=citation_text,
            source_id=source_id,
            kind="low_similarity",
            similarity_score=similarity,
        )

    def check_batch(
        self,
        citations: list[tuple[str, str, str]],
    ) -> tuple[list[HallucinationFlag], float]:
        """Sweep a batch of ``(citation_text, source_text, source_id)`` triples.

        Returns ``(flags, flag_rate)`` where ``flag_rate`` is
        ``len(flags) / len(citations)`` -- the per-batch operational metric
        consumed by the κ stop-hook gate. An empty batch returns
        ``([], 0.0)`` rather than dividing by zero.
        """

        flags: list[HallucinationFlag] = []
        for citation_text, source_text, source_id in citations:
            flag = self.check_citation(citation_text, source_text, source_id)
            if flag is not None:
                flags.append(flag)
        if not citations:
            return flags, 0.0
        return flags, len(flags) / len(citations)
