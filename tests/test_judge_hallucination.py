"""Tests for J6: judge-hallucination guard.

Background: in the V9 overlay finding from healthcraft, 73% of judge
disagreements were judge hallucinations -- the judge cited content that did
not literally appear in the source. The guard is the binding instrument for
production credibility of the L2 LLM judge.

The guard normalises whitespace and case, then performs:
  Step 1 -- literal substring check on the normalised citation
  Step 2 -- token Jaccard similarity fallback against a threshold

Outcomes:
  - literal substring match  -> None (no hallucination)
  - Jaccard >= threshold     -> None (paraphrase tolerated)
  - 0 < Jaccard < threshold  -> ``low_similarity`` flag
  - Jaccard == 0 + no literal-> ``literal_missing`` flag

``check_batch`` returns ``(flags_list, flag_rate)`` where ``flag_rate`` is
``len(flags) / len(citations)`` -- batch-level operational metric for the
stop-hook gate.
"""

from __future__ import annotations

from receipts.judge import HallucinationFlag, HallucinationGuard


def test_literal_substring_returns_none() -> None:
    guard = HallucinationGuard()
    assert guard.check_citation("foo bar", "this foo bar baz", "src-1") is None


def test_case_insensitive_literal_match() -> None:
    guard = HallucinationGuard()
    # Mixed-case citation should still match the lowercase source after
    # normalisation.
    assert guard.check_citation("FOO bar", "foo bar baz", "src-2") is None


def test_paraphrase_above_threshold_returns_none() -> None:
    guard = HallucinationGuard()
    # Citation shares >=40% tokens with source after tokenisation -> tolerated.
    citation = "patient reports chest pain and shortness of breath"
    source = "the patient reports chest pain and shortness of breath today"
    assert guard.check_citation(citation, source, "src-3") is None


def test_hallucination_low_similarity_flagged() -> None:
    guard = HallucinationGuard(similarity_threshold=0.40)
    # Some overlap ("chest pain"), but well below threshold against a long
    # source describing unrelated symptoms.
    citation = "severe chest pain radiating to left arm with diaphoresis"
    source = "the patient came in for a routine follow-up regarding chest pain"
    flag = guard.check_citation(citation, source, "src-4")
    assert flag is not None
    assert isinstance(flag, HallucinationFlag)
    assert flag.kind == "low_similarity"
    assert flag.source_id == "src-4"
    assert flag.citation_text == citation
    assert 0.0 < flag.similarity_score < 0.40


def test_literal_missing_zero_overlap() -> None:
    guard = HallucinationGuard()
    citation = "anaphylaxis with respiratory compromise"
    source = "ordered ibuprofen 400mg for headache"
    flag = guard.check_citation(citation, source, "src-5")
    assert flag is not None
    assert flag.kind == "literal_missing"
    assert flag.similarity_score == 0.0
    assert flag.source_id == "src-5"
    assert flag.citation_text == citation


def test_check_batch_returns_flag_rate() -> None:
    guard = HallucinationGuard(similarity_threshold=0.40)
    citations = [
        # 1: literal substring -> None
        ("foo bar", "this foo bar baz", "src-A"),
        # 2: paraphrase above threshold -> None
        (
            "patient reports chest pain and shortness of breath",
            "the patient reports chest pain and shortness of breath today",
            "src-B",
        ),
        # 3: zero overlap -> literal_missing
        ("anaphylaxis with respiratory compromise", "ordered ibuprofen 400mg", "src-C"),
        # 4: partial overlap below threshold -> low_similarity
        (
            "severe chest pain radiating to left arm with diaphoresis",
            "the patient came in for a routine follow-up regarding chest pain",
            "src-D",
        ),
    ]
    flags, flag_rate = guard.check_batch(citations)
    assert len(flags) == 2
    assert flag_rate == 0.5
    kinds = {f.kind for f in flags}
    assert kinds == {"literal_missing", "low_similarity"}


# --------------------------- additional coverage ---------------------------


def test_normalises_leading_trailing_punctuation() -> None:
    guard = HallucinationGuard()
    # Citation wrapped in quotes/commas; substring check should still hold.
    assert guard.check_citation('"foo bar",', "this foo bar baz", "src-6") is None


def test_collapses_whitespace_for_literal_match() -> None:
    guard = HallucinationGuard()
    # Tab- and double-space-separated citation collapses to single spaces
    # before substring check.
    citation = "foo\t bar"
    source = "lorem foo bar ipsum"
    assert guard.check_citation(citation, source, "src-7") is None


def test_check_batch_empty_returns_zero_rate() -> None:
    guard = HallucinationGuard()
    flags, rate = guard.check_batch([])
    assert flags == []
    assert rate == 0.0


def test_threshold_is_inclusive() -> None:
    # Threshold check is `>= threshold` -> exactly-at-threshold is tolerated.
    guard = HallucinationGuard(similarity_threshold=0.5)
    # Citation {a, b}, source {a, c} -> Jaccard = 1/3 ~ 0.333; flagged.
    flag = guard.check_citation("a b", "a c", "src-8")
    assert flag is not None
    assert flag.kind == "low_similarity"
    # Citation {a, b}, source {a, b, c} -> Jaccard = 2/3 ~ 0.667; tolerated.
    assert guard.check_citation("a b", "a b c", "src-9") is None
