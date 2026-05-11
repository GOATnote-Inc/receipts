"""P1-7: output emitter for the engineering weekly cycle.

The emitter consumes a :class:`~receipts.eng.reconciler.ReconcilerResult`
plus the live ledger ``Session`` and the three external-write connectors,
produces a single Markdown digest, and fans out:

  * one Linear comment per drifted epic — the comment body carries the
    revised acceptance criteria + drift summary so the VP Eng can scan
    the trail without opening the ledger,
  * one Slack DM to the VP Eng — Block Kit payload with a header + the
    top-3 drift items + a link reference,
  * one optional GitHub PR — the markdown body is the PR body and the
    head branch is ``receipts/<week_id>``.

``dry_run=True`` short-circuits every connector call so the same code
path can preview a week's outputs from the CLI without touching the
network. The Markdown is always built.

Drift detection
---------------
A draft is considered "drifted" when its ``drift_summary`` contains any
of the canonical drift markers (``scope-creep`` / ``scope-shrink`` /
``decision-not-reflected``) as a case-insensitive substring. This is
deliberately a substring match — the drafter prompt template
(P1-5) emits these exact tokens, but tolerance for surrounding text /
casing keeps the heuristic robust to small wording changes.

What this module deliberately does NOT do
-----------------------------------------
- No retry / backoff: the reconciler's replay layer owns idempotency.
- No Merkle append: external-write logging is handled by the connector
  invocation hooks one layer up (P1-8 CLI wires this).
- No CLI parsing: the CLI entrypoint is P1-8.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from receipts.ledger.exports import generate_markdown

if TYPE_CHECKING:
    from receipts.connectors import GitHubConnector, LinearConnector, SlackConnector
    from receipts.drafter.models import RevisedSpec
    from receipts.eng.reconciler import ReconcilerResult


__all__ = ["DRIFT_MARKERS", "EmitterResult", "emit_outputs"]


#: Substring markers (case-insensitive) the emitter scans for inside a
#: draft's ``drift_summary`` to decide whether the epic drifted.
DRIFT_MARKERS: tuple[str, ...] = (
    "scope-creep",
    "scope-shrink",
    "decision-not-reflected",
)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class EmitterResult:
    """Aggregate output of one :func:`emit_outputs` invocation.

    Attributes:
        markdown_body: The full Markdown digest. Always populated.
        linear_comment_ids: Comment ids returned by ``LinearConnector.add_comment``
            in the order the comments were posted (drifted-epics in
            ``external_id`` ASC order).
        slack_dm_ts: Slack message ``ts`` returned by ``send_dm``, or ``None``
            when Slack was not invoked.
        github_pr_url: ``html_url`` returned by ``create_pull_request``, or
            ``None`` when GitHub was not invoked.
        dry_run: Whether the run was a preview; mirrors the input flag.
    """

    markdown_body: str
    linear_comment_ids: list[str] = field(default_factory=list)
    slack_dm_ts: str | None = None
    github_pr_url: str | None = None
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_drifted(spec: RevisedSpec) -> bool:
    """``True`` iff the draft's ``drift_summary`` carries a drift marker."""
    summary = (spec.drift_summary or "").lower()
    return any(marker in summary for marker in DRIFT_MARKERS)


def _summary_block(result: ReconcilerResult) -> str:
    """Render the head-of-document summary block.

    Surfaces the audit-critical figures up front so a VP Eng reading the
    Markdown in Slack/email can decide whether to open the full digest:
    week_id, epic count, pass^1, κ, hallucination flag-rate, Merkle status.
    """
    parts: list[str] = []
    parts.append("# Receipts — Engineering Weekly Reconciliation")
    parts.append("")
    parts.append(f"- week_id: {result.week_id}")
    parts.append(f"- epic_count: {result.epic_count}")
    parts.append(f"- pass^1: {result.passk:.3f}")
    kappa = result.kappa
    parts.append(f"- kappa: {kappa:.3f}" if kappa is not None else "- kappa: n/a")
    rate = result.hallucination_flag_rate
    parts.append(
        f"- hallucination_flag_rate: {rate:.3f}"
        if rate is not None
        else "- hallucination_flag_rate: n/a"
    )
    merkle_status = "intact" if result.merkle_chain_intact else "BROKEN"
    parts.append(f"- merkle: {merkle_status} ({result.merkle_row_count} rows)")
    parts.append("")
    return "\n".join(parts)


def _linear_comment_body(spec: RevisedSpec) -> str:
    """Format the per-epic Linear comment.

    Shows the drift summary up front (the headline VP Eng cares about) and
    then the revised acceptance criteria so the team has the corrected
    statement of intent in the project thread itself.
    """
    lines: list[str] = []
    lines.append("Receipts: drift detected.")
    lines.append("")
    lines.append(f"Drift: {spec.drift_summary}")
    lines.append("")
    lines.append("Revised acceptance criteria:")
    for criterion in spec.acceptance_criteria:
        lines.append(f"- {criterion}")
    return "\n".join(lines)


def _slack_blocks(
    result: ReconcilerResult,
    drifted: list[tuple[str, RevisedSpec]],
) -> list[dict[str, Any]]:
    """Build a Slack Block Kit payload with header + top-3 drift fields.

    The blocks are pure Python ``dict``\\s — ``SlackConnector.send_dm`` will
    serialise them when it posts to ``chat.postMessage``. ``drifted`` is
    pre-sorted; we slice to the first three so the DM stays short.
    """
    top3 = drifted[:3]
    fields = []
    for ext_id, spec in top3:
        fields.append(
            {
                "type": "mrkdwn",
                "text": f"*{ext_id}*\n{spec.drift_summary}",
            }
        )

    header_text = f"Receipts week {result.week_id}: {len(drifted)} drifted epic(s)"
    summary_text = f"pass^1 {result.passk:.3f}" + (
        f" • kappa {result.kappa:.3f}" if result.kappa is not None else ""
    )

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header_text},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": summary_text},
        },
    ]
    if fields:
        blocks.append({"type": "section", "fields": fields})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Full digest: see receipts/{result.week_id} PR / markdown attachment.",
                }
            ],
        }
    )
    return blocks


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def emit_outputs(
    result: ReconcilerResult,
    session: Session,
    linear: LinearConnector | None = None,
    slack: SlackConnector | None = None,
    github: GitHubConnector | None = None,
    vp_eng_user_id: str | None = None,
    github_repo_for_pr: str | None = None,
    dry_run: bool = False,
) -> EmitterResult:
    """Emit the weekly digest + fan-out comments / DM / PR.

    Args:
        result: The reconciler output for the week.
        session: An open SQLAlchemy ``Session`` against the same L1 ledger
            the reconciler wrote into. Used to render the Markdown digest.
        linear: Optional :class:`LinearConnector`. When supplied (and
            ``dry_run`` is false), a comment is posted on every drifted
            epic.
        slack: Optional :class:`SlackConnector`. When supplied alongside a
            ``vp_eng_user_id`` (and ``dry_run`` is false), a single DM is
            posted with a Block Kit payload covering the top-3 drift
            items.
        github: Optional :class:`GitHubConnector`. When supplied alongside
            ``github_repo_for_pr`` (and ``dry_run`` is false), a PR is
            opened with the Markdown digest as its body.
        vp_eng_user_id: Slack user id to DM. Required for the Slack path
            but the function never raises — missing → DM skipped.
        github_repo_for_pr: ``owner/repo`` slug for the PR. Required for
            the GitHub path; missing → PR skipped.
        dry_run: When true, the Markdown is built but every external
            connector call is skipped.

    Returns:
        :class:`EmitterResult` covering the Markdown body and any
        connector receipts (comment ids, Slack ``ts``, PR URL).
    """
    # ---- Step 1: Markdown -------------------------------------------------
    summary = _summary_block(result)
    body = generate_markdown(session)
    markdown_body = f"{summary}{body}" if body else summary

    out = EmitterResult(markdown_body=markdown_body, dry_run=dry_run)

    # Identify drifted drafts once; reuse for Linear + Slack.
    drifted: list[tuple[str, RevisedSpec]] = [
        (ext_id, spec) for ext_id, spec in result.drafts if _is_drifted(spec)
    ]
    # Deterministic order: ascending by epic external_id. The reconciler
    # already emits drafts in this order, but sorting locally keeps the
    # emitter independent of upstream invariants.
    drifted.sort(key=lambda pair: pair[0])

    if dry_run:
        return out

    # ---- Step 2: Linear per-epic comments --------------------------------
    if linear is not None:
        for ext_id, spec in drifted:
            comment_id = linear.add_comment(ext_id, _linear_comment_body(spec))
            out.linear_comment_ids.append(comment_id)

    # ---- Step 3: Slack DM ------------------------------------------------
    if slack is not None and vp_eng_user_id is not None:
        blocks = _slack_blocks(result, drifted)
        out.slack_dm_ts = slack.send_dm(vp_eng_user_id, blocks)

    # ---- Step 4: GitHub PR -----------------------------------------------
    if github is not None and github_repo_for_pr is not None:
        out.github_pr_url = github.create_pull_request(
            github_repo_for_pr,
            f"receipts: week {result.week_id} reconciliation",
            markdown_body,
            "main",
            f"receipts/{result.week_id}",
        )

    return out
