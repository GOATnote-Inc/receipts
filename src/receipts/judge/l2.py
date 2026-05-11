"""L2 LLM judge wrapper + version registry (J4).

L2 is the only non-deterministic layer of the CEIS judge stack. ``LLMJudge``
is a thin shim that:

1. Routes a call through ``ReplayStore`` (default mode: replay) so
   ``make test`` is hermetic by construction -- live API hits require an
   explicit ``RECEIPTS_JUDGE_MODE=record`` opt-in.
2. Dispatches record-mode calls to vendor-specific adapters
   (:class:`AnthropicAdapter`, :class:`OpenAIAdapter`) that enforce the
   model-family kwargs contract -- ``claude-opus-4-7`` rejects
   ``temperature``/``top_p``/``top_k``/``budget_tokens``;
   ``gpt-5.4-*`` rejects ``max_tokens`` in favour of
   ``max_completion_tokens``.
3. Parses every raw response through :class:`JudgeOutput` (pydantic v2)
   so schema-drift surfaces at the wrapper, not three layers downstream.
4. Stamps each invocation onto the Merkle log via :class:`MerkleLog` --
   model + prompt_sha + request_hash + response_text + latency + cost --
   so auditors can correlate any verdict to the exact prompt that
   produced it. ``prompt_sha`` is the version-registry primary key.

The wrapper deliberately keeps SDK clients injectable so the test suite
exercises both the replay and record codepaths without touching the
network.
"""

from __future__ import annotations

import hashlib
import json
import time

from pydantic import BaseModel, Field

from receipts.judge.replay import JudgeCall, ReplayStore, stable_hash
from receipts.ledger.merkle import MerkleLog


class JudgeOutput(BaseModel):
    """Validated LLM judge verdict.

    Schema is intentionally narrow: a single score on [0, 1], a required
    rationale long enough to be meaningful (>=10 chars catches "ok"-style
    null verdicts), and an open-ended flags list for downstream taxonomy
    work. Drift in any of these surfaces as a pydantic ``ValidationError``
    at the wrapper, not at the call site.
    """

    score: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=10)
    flags: list[str] = Field(default_factory=list)


class AnthropicAdapter:
    """Record-mode adapter for the Anthropic Messages API.

    Enforces the ``claude-opus-4-7`` kwargs contract by construction:
    only ``model``, ``max_tokens``, ``messages`` are ever passed.
    Any future temptation to add sampler-tuning kwargs lives or dies
    in this method.
    """

    @classmethod
    def call(
        cls,
        client,
        model: str,
        prompt: str,
        input_payload: dict,
        max_tokens: int = 1024,
    ) -> tuple[str, int, float]:
        """Invoke ``client.messages.create`` and return ``(raw, latency_ms, cost_usd)``.

        ``cost_usd`` is a 0.0 stub for now -- vendor billing reconciliation
        lives in a separate task. ``latency_ms`` is wall-clock elapsed so
        downstream perf dashboards see a real number, not a placeholder.
        """
        content = prompt + "\n\nINPUT:\n" + json.dumps(input_payload)
        started = time.monotonic()
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": content}],
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        raw = response.content[0].text
        return raw, latency_ms, 0.0


class OpenAIAdapter:
    """Record-mode adapter for the OpenAI Chat Completions API.

    ``gpt-5.4-*`` requires ``max_completion_tokens``; the legacy
    ``max_tokens`` kwarg is rejected. This adapter pins that contract.
    """

    @classmethod
    def call(
        cls,
        client,
        model: str,
        prompt: str,
        input_payload: dict,
        max_completion_tokens: int = 1024,
    ) -> tuple[str, int, float]:
        """Invoke ``client.chat.completions.create`` and return ``(raw, latency_ms, cost_usd)``."""
        content = prompt + "\n\nINPUT:\n" + json.dumps(input_payload)
        started = time.monotonic()
        response = client.chat.completions.create(
            model=model,
            max_completion_tokens=max_completion_tokens,
            messages=[{"role": "user", "content": content}],
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        raw = response.choices[0].message.content
        return raw, latency_ms, 0.0


class LLMJudge:
    """Thin record/replay wrapper around a model-family adapter.

    The wrapper is the only object the rest of the pipeline talks to. It
    owns: replay-mode dispatch, record-mode persistence, schema validation
    of the response, and the Merkle log attestation. SDK clients are
    injected (not constructed) so tests can mock both vendors without
    touching their constructors.
    """

    def __init__(
        self,
        model: str,
        prompt_template: str,
        replay_store: ReplayStore,
        merkle_log: MerkleLog | None = None,
        anthropic_client=None,
        openai_client=None,
    ) -> None:
        self.model = model
        self.prompt_template = prompt_template
        self.replay_store = replay_store
        self.merkle_log = merkle_log
        self._anthropic_client = anthropic_client
        self._openai_client = openai_client

    @property
    def prompt_sha(self) -> str:
        """sha256 hex of the prompt template -- the version-registry primary key.

        Auditors recompute this against the on-disk prompt to confirm a
        Merkle-logged verdict was produced by the prompt they expect.
        Stored hex-only (64 chars) so it slots cleanly into the existing
        ``judge_rationale.prompt_sha`` and ``attestation.payload`` columns.
        """
        return hashlib.sha256(self.prompt_template.encode("utf-8")).hexdigest()

    def evaluate(
        self,
        input_payload: dict,
        temperature: float = 0.0,
        seed: int | None = None,
    ) -> JudgeOutput:
        """Score ``input_payload`` and return a validated ``JudgeOutput``.

        Flow:
        1. Build the canonical ``JudgeCall`` so replay key is deterministic.
        2. ``replay`` mode: load the recording, take ``response["text"]``.
        3. ``record`` mode: dispatch to the model-family adapter, then
           persist the recording (so the next replay is hermetic).
        4. Parse via ``JudgeOutput.model_validate_json`` -- raises pydantic
           ``ValidationError`` on schema drift.
        5. Optionally append a Merkle row tying together model,
           prompt_sha, request_hash, response_text, latency, cost.
        """
        call = JudgeCall(
            model=self.model,
            prompt=self.prompt_template,
            temperature=temperature,
            seed=seed,
            input_payload=input_payload,
        )
        mode = ReplayStore.mode_from_env()

        if mode == "replay":
            rec = self.replay_store.replay(call)
            raw = rec.response["text"]
            latency_ms = rec.latency_ms
            cost_usd = rec.cost_usd
        else:
            raw, latency_ms, cost_usd = self._dispatch_record(call)
            self.replay_store.record(
                call,
                response={"text": raw},
                latency_ms=latency_ms,
                cost_usd=cost_usd,
            )

        output = JudgeOutput.model_validate_json(raw)

        if self.merkle_log is not None:
            self.merkle_log.append(
                {
                    "model": self.model,
                    "prompt_sha": self.prompt_sha,
                    "request_hash": stable_hash(call),
                    "response_text": raw,
                    "latency_ms": latency_ms,
                    "cost_usd": cost_usd,
                },
                kind="judge_call",
                target_id=0,
                target_kind="judge",
            )

        return output

    # ----------------------- internal helpers -----------------------

    def _dispatch_record(self, call: JudgeCall) -> tuple[str, int, float]:
        """Route a record-mode call to the right model-family adapter.

        We pick by model-name prefix rather than a registry: ``claude-*``
        -> Anthropic, ``gpt-*`` -> OpenAI. Any other family is a hard
        ``ValueError`` -- silently falling back would let an unsupported
        model produce un-attested verdicts.
        """
        if self.model.startswith("claude-"):
            if self._anthropic_client is None:
                raise ValueError(f"record mode requires anthropic_client for model {self.model}")
            return AnthropicAdapter.call(
                self._anthropic_client,
                self.model,
                self.prompt_template,
                call.input_payload,
            )
        if self.model.startswith("gpt-"):
            if self._openai_client is None:
                raise ValueError(f"record mode requires openai_client for model {self.model}")
            return OpenAIAdapter.call(
                self._openai_client,
                self.model,
                self.prompt_template,
                call.input_payload,
            )
        raise ValueError(f"unsupported model family: {self.model}")


__all__ = [
    "AnthropicAdapter",
    "JudgeOutput",
    "LLMJudge",
    "OpenAIAdapter",
]
