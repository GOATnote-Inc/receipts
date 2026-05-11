"""P1-7: output-emitter tests.

The emitter consumes a :class:`ReconcilerResult` plus the live ledger session
and the three external-write connectors (Linear / Slack / GitHub), produces a
Markdown digest, and fans out per-epic Linear comments + a single Slack DM
+ (optionally) a GitHub PR. ``dry_run=True`` short-circuits every connector
call so the same code path can drive previews from the CLI.

Test discipline
---------------
- No real network: every connector is ``MagicMock(spec=<Connector>)``.
- No real reconciler run: we hand-build a small :class:`ReconcilerResult`
  with 3-5 fake drafts so the test surface stays pinned on emitter logic.
- An in-memory SQLite DB is upgraded to ``alembic head`` and seeded with the
  epics referenced by the fake drafts so ``generate_markdown`` can resolve
  them.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from receipts.connectors import GitHubConnector, LinearConnector, SlackConnector
from receipts.drafter.models import Citation, RevisedSpec
from receipts.eng import EmitterResult, emit_outputs
from receipts.eng.reconciler import ReconcilerResult
from receipts.ledger.models import Epic

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


# ---------------------------------------------------------------------------
# Fixtures: in-memory ledger + a hand-built ReconcilerResult
# ---------------------------------------------------------------------------


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'emitter.db'}"


@pytest.fixture
def session(db_url: str) -> Iterator[Session]:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(cfg, "head")
    engine = create_engine(db_url)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()


def _seed_epic(session: Session, external_id: str, title: str) -> None:
    t0 = datetime(2026, 5, 1, tzinfo=UTC).replace(tzinfo=None)
    session.add(
        Epic(
            external_id=external_id,
            title=title,
            acceptance_criteria=[f"{external_id} criterion"],
            created_at=t0,
            updated_at=t0,
        )
    )
    session.commit()


def _draft(criterion: str, drift_summary: str) -> RevisedSpec:
    """Build a tiny RevisedSpec citing one fake PR."""
    return RevisedSpec(
        acceptance_criteria=[criterion],
        citations={criterion: [Citation(artifact_kind="pr", external_id="PR-1")]},
        drift_summary=drift_summary,
    )


def _result_with_drafts(
    drafts: list[tuple[str, RevisedSpec]],
    *,
    passk: float = 0.8,
    kappa: float | None = 0.55,
    hallucination_flag_rate: float | None = 0.02,
) -> ReconcilerResult:
    return ReconcilerResult(
        week_id="week_0001",
        drafts=drafts,
        epic_count=len(drafts),
        pass_count=int(round(passk * len(drafts))),
        passk=passk,
        kappa=kappa,
        hallucination_flag_rate=hallucination_flag_rate,
        merkle_chain_intact=True,
        merkle_row_count=len(drafts),
    )


@pytest.fixture
def seeded_result(session: Session) -> ReconcilerResult:
    """3 epics: two drifted, one clean."""
    _seed_epic(session, "EPIC-0001", "scope creep epic")
    _seed_epic(session, "EPIC-0002", "scope shrink epic")
    _seed_epic(session, "EPIC-0003", "clean ship epic")
    drafts = [
        (
            "EPIC-0001",
            _draft("Implement A", "EPIC-0001: scope-creep — extra criterion shipped."),
        ),
        (
            "EPIC-0002",
            _draft("Implement B", "EPIC-0002: scope-shrink — criterion dropped."),
        ),
        (
            "EPIC-0003",
            _draft("Implement C", "EPIC-0003: shipped as scoped — no drift."),
        ),
    ]
    return _result_with_drafts(drafts)


def _mock_linear() -> MagicMock:
    linear = MagicMock(spec=LinearConnector)
    linear.add_comment.side_effect = lambda epic_external_id, body: f"comment-{epic_external_id}"
    return linear


def _mock_slack() -> MagicMock:
    slack = MagicMock(spec=SlackConnector)
    slack.send_dm.return_value = "1700000000.000123"
    return slack


def _mock_github() -> MagicMock:
    github = MagicMock(spec=GitHubConnector)
    github.create_pull_request.return_value = "https://github.com/acme/receipts/pull/42"
    return github


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_emit_outputs_dry_run_skips_all_connector_calls(
    session: Session, seeded_result: ReconcilerResult
) -> None:
    """dry_run=True must short-circuit every external write."""
    linear = _mock_linear()
    slack = _mock_slack()
    github = _mock_github()

    out = emit_outputs(
        seeded_result,
        session,
        linear=linear,
        slack=slack,
        github=github,
        vp_eng_user_id="U_VPENG",
        github_repo_for_pr="acme/receipts",
        dry_run=True,
    )

    linear.add_comment.assert_not_called()
    slack.send_dm.assert_not_called()
    github.create_pull_request.assert_not_called()
    assert isinstance(out, EmitterResult)
    assert out.dry_run is True
    assert out.markdown_body  # markdown still built
    assert out.linear_comment_ids == []
    assert out.slack_dm_ts is None
    assert out.github_pr_url is None


def test_emit_outputs_markdown_contains_summary_block(
    session: Session, seeded_result: ReconcilerResult
) -> None:
    """The summary block at the head must surface week_id, passk, κ, halluc rate."""
    out = emit_outputs(seeded_result, session, dry_run=True)

    md = out.markdown_body
    assert "week_0001" in md
    # passk = 0.8 → "0.800" or "0.80" formatting is fine; we accept either.
    assert "0.80" in md
    # κ value
    assert "0.55" in md
    # hallucination rate
    assert "0.02" in md
    # merkle integrity tag
    assert "merkle" in md.lower()
    # epic count
    assert "3" in md


def test_emit_outputs_linear_comments_only_on_drift(
    session: Session, seeded_result: ReconcilerResult
) -> None:
    """Drifted epics (scope-creep, scope-shrink) get comments; clean ones don't."""
    linear = _mock_linear()

    out = emit_outputs(seeded_result, session, linear=linear, dry_run=False)

    assert linear.add_comment.call_count == 2
    posted_ids = sorted(call.kwargs.get("epic_external_id") or call.args[0]
                        for call in linear.add_comment.call_args_list)
    assert posted_ids == ["EPIC-0001", "EPIC-0002"]
    assert out.linear_comment_ids == ["comment-EPIC-0001", "comment-EPIC-0002"]


def test_emit_outputs_slack_dm_payload_has_top_3(
    session: Session,
) -> None:
    """Slack blocks include up to 3 drift items (in deterministic order)."""
    # Seed 5 epics, 4 drifted + 1 clean — emitter must clip to top 3.
    _seed_epic(session, "EPIC-0001", "creep 1")
    _seed_epic(session, "EPIC-0002", "creep 2")
    _seed_epic(session, "EPIC-0003", "shrink 1")
    _seed_epic(session, "EPIC-0004", "decision missed")
    _seed_epic(session, "EPIC-0005", "clean")
    drafts = [
        ("EPIC-0001", _draft("A", "EPIC-0001: scope-creep added X.")),
        ("EPIC-0002", _draft("B", "EPIC-0002: scope-creep added Y.")),
        ("EPIC-0003", _draft("C", "EPIC-0003: scope-shrink dropped Z.")),
        (
            "EPIC-0004",
            _draft("D", "EPIC-0004: decision-not-reflected — meeting outcome missed."),
        ),
        ("EPIC-0005", _draft("E", "EPIC-0005: shipped as scoped — no drift.")),
    ]
    result = _result_with_drafts(drafts, passk=0.6)
    slack = _mock_slack()

    out = emit_outputs(
        result,
        session,
        slack=slack,
        vp_eng_user_id="U_VPENG",
        dry_run=False,
    )

    slack.send_dm.assert_called_once()
    call = slack.send_dm.call_args
    blocks = call.kwargs.get("blocks") if "blocks" in call.kwargs else call.args[1]
    user_id = call.kwargs.get("user_id") if "user_id" in call.kwargs else call.args[0]
    assert user_id == "U_VPENG"
    rendered = repr(blocks)
    # First 3 drifted (by external_id ASC): EPIC-0001, EPIC-0002, EPIC-0003.
    assert "EPIC-0001" in rendered
    assert "EPIC-0002" in rendered
    assert "EPIC-0003" in rendered
    # The fourth drifted should not appear; clean epic should not appear.
    assert "EPIC-0004" not in rendered
    assert "EPIC-0005" not in rendered
    assert out.slack_dm_ts == "1700000000.000123"


def test_emit_outputs_github_pr_when_repo_provided(
    session: Session, seeded_result: ReconcilerResult
) -> None:
    """github.create_pull_request must be invoked with the canonical signature."""
    github = _mock_github()

    out = emit_outputs(
        seeded_result,
        session,
        github=github,
        github_repo_for_pr="acme/receipts",
        dry_run=False,
    )

    github.create_pull_request.assert_called_once()
    call = github.create_pull_request.call_args
    kwargs = {**call.kwargs}
    args = call.args
    repo = kwargs.get("repo") if "repo" in kwargs else args[0]
    title = kwargs.get("title") if "title" in kwargs else args[1]
    body = kwargs.get("body") if "body" in kwargs else args[2]
    base = kwargs.get("base") if "base" in kwargs else args[3]
    head = kwargs.get("head") if "head" in kwargs else args[4]
    assert repo == "acme/receipts"
    assert title == "receipts: week week_0001 reconciliation"
    assert body == out.markdown_body
    assert base == "main"
    assert head == "receipts/week_0001"
    assert out.github_pr_url == "https://github.com/acme/receipts/pull/42"


def test_emit_outputs_returns_emitter_result_with_ids(
    session: Session, seeded_result: ReconcilerResult
) -> None:
    """All three connector results land on the returned EmitterResult."""
    linear = _mock_linear()
    slack = _mock_slack()
    github = _mock_github()

    out = emit_outputs(
        seeded_result,
        session,
        linear=linear,
        slack=slack,
        github=github,
        vp_eng_user_id="U_VPENG",
        github_repo_for_pr="acme/receipts",
        dry_run=False,
    )

    assert isinstance(out, EmitterResult)
    assert out.dry_run is False
    assert out.markdown_body.startswith("#")
    assert out.linear_comment_ids == ["comment-EPIC-0001", "comment-EPIC-0002"]
    assert out.slack_dm_ts == "1700000000.000123"
    assert out.github_pr_url == "https://github.com/acme/receipts/pull/42"


def test_emit_outputs_no_connectors_returns_markdown_only(
    session: Session, seeded_result: ReconcilerResult
) -> None:
    """Without any connector instances the function still builds the markdown."""
    out = emit_outputs(seeded_result, session)

    assert out.dry_run is False
    assert "week_0001" in out.markdown_body
    assert out.linear_comment_ids == []
    assert out.slack_dm_ts is None
    assert out.github_pr_url is None
