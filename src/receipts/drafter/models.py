"""Pydantic v2 models for the revised-spec drafter.

These models pin the wire shape that the drafter emits and the validator
checks. They're deliberately small and dependency-free so downstream teams
(judge, ledger, connectors) can import them without dragging in LLM stacks.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ArtifactKind = Literal["pr", "meeting", "thread"]


class PRRef(BaseModel):
    """A pull request observed in the sprint's execution window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    external_id: str
    repo: str
    number: int
    diff_summary: str


class MeetingRef(BaseModel):
    """A meeting whose recorded decisions are part of the execution context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    external_id: str
    decisions: list[str]


class ThreadRef(BaseModel):
    """A chat thread (Slack/etc.) summarized into the execution context."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    external_id: str
    channel: str
    summary: str


class Epic(BaseModel):
    """The original intent: ticket id + the criteria we promised to ship."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int
    external_id: str
    title: str
    acceptance_criteria: list[str]


class Execution(BaseModel):
    """What actually happened: PRs landed, meetings held, threads resolved."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prs: list[PRRef] = Field(default_factory=list)
    meetings: list[MeetingRef] = Field(default_factory=list)
    threads: list[ThreadRef] = Field(default_factory=list)


class Citation(BaseModel):
    """A pointer from a revised criterion back to a source artifact.

    `locator` is an optional fine-grained reference within the artifact —
    a diff line range, a decision index, a thread anchor, etc.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_kind: ArtifactKind
    external_id: str
    locator: str | None = None


class RevisedSpec(BaseModel):
    """The drafter's output: criteria rewritten to match what shipped.

    `citations` is keyed by the exact criterion text (str → list[Citation]).
    The validator enforces that every emitted criterion has ≥1 citation
    backed by an artifact id present in the input Execution.
    """

    model_config = ConfigDict(extra="forbid")

    acceptance_criteria: list[str]
    citations: dict[str, list[Citation]] = Field(default_factory=dict)
    drift_summary: str
