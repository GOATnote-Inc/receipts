"""Real-LLM drafter path (P1-5).

The S1 stub registry (``EPIC-001..030``) ships canned RevisedSpec values so
the substrate suite stays deterministic. P1-5 adds an opt-in branch for
epics outside the registry: the caller supplies an :class:`LLMJudge` and
the drafter routes ``draft_revised_spec_llm`` through it.

Why tunnel the spec through ``JudgeOutput.rationale``
-----------------------------------------------------
``LLMJudge`` is the only sanctioned LLM entrypoint in the codebase. It
already owns:

- ``ReplayStore`` for hermetic ``make test`` runs
- ``prompt_sha`` version registry (the sha256 of the prompt template)
- Merkle log attestation on every call (J4)
- Adapter-level kwargs contracts for Claude vs GPT model families

Adding a parallel "drafter LLM client" would duplicate every one of
those concerns. Instead, the prompt instructs the model to emit a
JSON-encoded RevisedSpec inside the ``rationale`` field of the existing
``JudgeOutput`` schema. The drafter parses ``rationale`` as JSON and
constructs the ``RevisedSpec``. ``score`` and ``flags`` are unused by
this path but preserved so the wire schema is unchanged.

Failure modes
-------------
- Rationale is not JSON → ``ValueError`` (caller decides whether to
  retry / surface to a human).
- Rationale parses to JSON but fails pydantic ``RevisedSpec`` schema →
  ``pydantic.ValidationError`` propagates from ``model_validate``.
- Citations reference phantom artifacts → the downstream
  ``validate_revised_spec`` catches it; this module does not silently
  filter outputs.
"""

from __future__ import annotations

import json
from pathlib import Path

from receipts.drafter.models import EncounterContract, EncounterStub, Epic, Execution, RevisedSpec
from receipts.judge.l2 import LLMJudge

_PROMPTS_DIR = Path(__file__).parent / "prompts"

#: Prompt template used by ``draft_revised_spec_llm``.
#:
#: This string is the version-registry primary key: ``LLMJudge.prompt_sha``
#: is ``sha256(REVISED_SPEC_PROMPT_TEMPLATE.encode())``. Auditors recompute
#: that hex against every Merkle-logged judge_call row to confirm the
#: deployed prompt matches what was attested. Any non-cosmetic edit here
#: invalidates existing recordings — by design.
REVISED_SPEC_PROMPT_TEMPLATE = (
    "You are the revised-spec drafter for an append-only intent-vs-execution "
    "attestation ledger. You will receive (a) an Epic — the original intent "
    "stating acceptance_criteria the team promised to ship — and (b) an "
    "Execution snapshot listing the PRs, meetings, and threads that actually "
    "occurred during the sprint.\n\n"
    "Your job is to emit a RevisedSpec — the spec rewritten to match what "
    "actually shipped — with citations back to source artifacts and a drift "
    "summary explaining any deviation.\n\n"
    "OUTPUT CONTRACT (strict):\n"
    "Respond with a single JSON object that matches the JudgeOutput schema:\n"
    "{\n"
    '  "score": 1.0,\n'
    '  "rationale": "<JSON-STRING>",\n'
    '  "flags": []\n'
    "}\n\n"
    "The rationale field MUST be a JSON-encoded string whose decoded value "
    "matches the RevisedSpec schema:\n"
    "{\n"
    '  "acceptance_criteria": [<string>, ...],\n'
    '  "citations": {\n'
    '    "<criterion text>": [\n'
    '      {"artifact_kind": "pr"|"meeting"|"thread", "external_id": "<id>", "locator": "<opt>"|null}\n'
    "    ]\n"
    "  },\n"
    '  "drift_summary": "<string>"\n'
    "}\n\n"
    "Rules:\n"
    "1. Every acceptance_criteria entry MUST appear as a key in citations "
    "with at least one Citation referencing an artifact present in the input "
    "Execution. No phantom artifacts.\n"
    "2. Every citation external_id MUST match an external_id in the "
    "Execution under the same artifact_kind.\n"
    "3. drift_summary MUST be non-empty. If nothing drifted, say "
    "'shipped as scoped' and cite the deciding meeting.\n"
    "4. Do not invent PRs / meetings / threads that are not in the input.\n"
    "5. The outer JudgeOutput fields (score, flags) are not used by the "
    "drafter; set score=1.0 and flags=[] unless instructed otherwise."
)


#: Prompt template used by ``draft_encounter_contract_llm``.
#:
#: Loaded from ``prompts/encounter_contract.txt`` at module-load time so the
#: source-of-truth lives in a reviewable, diffable file alongside the other
#: drafter assets rather than as an inline string. The ``LLMJudge.prompt_sha``
#: for this template is ``sha256(ENCOUNTER_CONTRACT_PROMPT_TEMPLATE.encode())``;
#: auditors recompute that hex against every Merkle-logged judge_call row to
#: confirm the deployed prompt matches what was attested. Any non-cosmetic
#: edit to the txt file invalidates existing recordings — by design.
ENCOUNTER_CONTRACT_PROMPT_TEMPLATE = (_PROMPTS_DIR / "encounter_contract.txt").read_text(
    encoding="utf-8"
)


def _parse_rationale_to_revised_spec(rationale: str) -> RevisedSpec:
    """Decode ``rationale`` (a JSON string) into a ``RevisedSpec``.

    The judge schema requires ``rationale`` to be a string; the drafter
    further requires that string to be valid JSON matching the
    RevisedSpec schema. We raise ``ValueError`` (with the offending
    prefix elided to a manageable size) when either contract is
    violated, so the caller — and the test suite — can distinguish a
    "model emitted prose" failure from a deeper schema bug.
    """
    try:
        decoded = json.loads(rationale)
    except json.JSONDecodeError as exc:
        snippet = rationale if len(rationale) <= 120 else rationale[:120] + "..."
        raise ValueError(
            f"LLM judge rationale is not valid JSON; cannot parse as "
            f"RevisedSpec. snippet={snippet!r}"
        ) from exc

    if not isinstance(decoded, dict):
        raise ValueError(
            "LLM judge rationale parsed to a non-object JSON value; "
            f"expected a RevisedSpec object, got {type(decoded).__name__}."
        )

    return RevisedSpec.model_validate(decoded)


def draft_revised_spec_llm(
    epic: Epic,
    execution: Execution,
    judge: LLMJudge,
) -> RevisedSpec:
    """Draft a RevisedSpec by routing through an ``LLMJudge``.

    Build a canonical ``input_payload`` from ``epic`` + ``execution``,
    call ``judge.evaluate`` (which handles replay vs record + Merkle
    attestation), and parse the ``rationale`` JSON into a RevisedSpec.

    The returned spec is **not** pre-validated against the Execution —
    callers should invoke ``validate_revised_spec`` themselves so phantom
    citations surface as a ValidationError (matching the behavior of the
    stub registry path through ``draft_revised_spec``).
    """
    input_payload = {
        "epic": epic.model_dump(),
        "execution": execution.model_dump(),
    }
    judge_output = judge.evaluate(input_payload)
    return _parse_rationale_to_revised_spec(judge_output.rationale)


def _parse_rationale_to_encounter_contract(rationale: str) -> EncounterContract:
    """Decode ``rationale`` (a JSON string) into an ``EncounterContract``.

    Mirrors ``_parse_rationale_to_revised_spec`` on the clinical side:
    the judge schema requires ``rationale`` to be a string; the drafter
    further requires that string to be valid JSON matching the
    EncounterContract schema. ``ValueError`` (with the offending prefix
    elided) surfaces "model emitted prose"; ``pydantic.ValidationError``
    propagates for deeper schema bugs. The safety-floor check
    (``safety_criteria`` non-empty) is enforced downstream by
    ``validate_encounter_contract``, not here — this parser refuses to
    silently mutate model output.
    """
    try:
        decoded = json.loads(rationale)
    except json.JSONDecodeError as exc:
        snippet = rationale if len(rationale) <= 120 else rationale[:120] + "..."
        raise ValueError(
            f"LLM judge rationale is not valid JSON; cannot parse as "
            f"EncounterContract. snippet={snippet!r}"
        ) from exc

    if not isinstance(decoded, dict):
        raise ValueError(
            "LLM judge rationale parsed to a non-object JSON value; "
            f"expected an EncounterContract object, got {type(decoded).__name__}."
        )

    return EncounterContract.model_validate(decoded)


def draft_encounter_contract_llm(
    stub: EncounterStub,
    judge: LLMJudge,
) -> EncounterContract:
    """Draft an EncounterContract by routing through an ``LLMJudge``.

    Build a canonical ``input_payload`` from ``stub``, call
    ``judge.evaluate`` (which handles replay vs record + Merkle
    attestation), and parse the ``rationale`` JSON into an
    EncounterContract.

    The returned contract is **not** pre-validated — callers should
    invoke ``validate_encounter_contract`` themselves so missing safety
    criteria, phantom artifact kinds, and uncited criteria surface as
    a ValidationError (matching the behavior of the stub registry path
    through ``draft_encounter_contract``).
    """
    input_payload = {"stub": stub.model_dump()}
    judge_output = judge.evaluate(input_payload)
    return _parse_rationale_to_encounter_contract(judge_output.rationale)


__all__ = [
    "ENCOUNTER_CONTRACT_PROMPT_TEMPLATE",
    "REVISED_SPEC_PROMPT_TEMPLATE",
    "draft_encounter_contract_llm",
    "draft_revised_spec_llm",
]
