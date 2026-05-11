"""P1-9: Phase 1 end-to-end weekly-cycle test.

This is the closing assertion for the Engineering Receipts vertical. Every
prior P1 task (P1-1..P1-8) ships a unit-scoped suite that pins one layer in
isolation; this test wires the entire vertical together with MagicMock
connectors, drives one reconcile + emit cycle against
``fixtures/eng/week_0001``, and asserts that the canonical Phase 1 invariants
all hold simultaneously:

(a) **Reconciliation produces a clean ``ReconcilerResult``.**
    epic_count == 30, pass_count == 30, passk == 1.0, merkle_chain_intact.
(b) **Emitter produces a fully-populated ``EmitterResult``.**
    Non-empty markdown_body; linear_comment_ids count matches the number of
    drifted epics; slack_dm_ts present; github_pr_url present.
(c) **Markdown is byte-stable across two consecutive runs.**
    Two fresh in-memory ledgers, two reconcile + emit cycles, byte-identical
    ``markdown_body``. This is the determinism gate the VP-Eng-facing
    Markdown digest contracts on.
(d) **Connector write methods were called with the expected payloads.**
    Linear ``add_comment`` per drifted epic, Slack ``send_dm`` once with the
    VP Eng user id + Block Kit list, GitHub ``create_pull_request`` once
    with the canonical title / base / head pair.
(e) **Hallucination flag rate is 0.**
    The stub-drafter cites synthetic PR / MTG / THR ids that are surfaced
    through the reconciler's stub-citation bridge, so every citation
    resolves to an artifact present in the Execution that scored it.
(f) **Dual-judge path is deliberately skipped.**
    Phase 1 ships the stub drafter; the real-LLM dual-judge path lives
    behind J4 / J7 and is exercised by P1-5 unit tests, not by this
    Phase-1 closing test. Wiring a ``DualJudge`` here would just re-test
    P1-5's stub-replay surface without exercising any new seam.

Runtime budget: <30s total. Stub drafter only — no LLM calls.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from receipts.connectors import GitHubConnector, LinearConnector, SlackConnector
from receipts.eng import EmitterResult, ReconcilerResult, emit_outputs, reconcile_week
from receipts.judge.hallucination_guard import HallucinationGuard
from receipts.ledger.merkle import MerkleLog

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
WEEK_DIR = REPO_ROOT / "fixtures" / "eng" / "week_0001"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    """One SQLite file per test; alembic-bootstrapped to ``head``."""
    return f"sqlite:///{tmp_path / 'phase1_e2e.db'}"


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


def _mock_linear() -> MagicMock:
    """LinearConnector mock whose ``add_comment`` returns a stable per-epic id."""
    linear = MagicMock(spec=LinearConnector)
    linear.add_comment.side_effect = lambda epic_external_id, body: (
        f"linear-comment-{epic_external_id}"
    )
    return linear


def _mock_slack() -> MagicMock:
    slack = MagicMock(spec=SlackConnector)
    slack.send_dm.return_value = "1700000000.000999"
    return slack


def _mock_github() -> MagicMock:
    github = MagicMock(spec=GitHubConnector)
    github.create_pull_request.return_value = "https://github.com/acme/receipts/pull/9001"
    return github


def _drifted_epic_ids(result: ReconcilerResult) -> list[str]:
    """Replicate the emitter's drift detection so call-count assertions stay tight.

    The emitter's drift heuristic is a substring match on the canonical
    drift markers (``scope-creep`` / ``scope-shrink`` /
    ``decision-not-reflected``). Recomputing it here keeps the assertion
    independent of internal emitter state and lets the test detect a
    drift-heuristic regression as a failed call-count match rather than a
    silent miscount.
    """
    from receipts.eng.emitter import DRIFT_MARKERS

    return sorted(
        ext_id
        for ext_id, spec in result.drafts
        if any(marker in (spec.drift_summary or "").lower() for marker in DRIFT_MARKERS)
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_phase1_full_cycle_with_all_connectors(session: Session) -> None:
    """End-to-end weekly cycle with every external connector mocked.

    Asserts the 5 active Phase 1 invariants on a single reconcile + emit pass:

    * reconciliation result counts + passk + merkle chain (invariant a),
    * emitter result populated on every channel (invariant b),
    * connector write methods called with the expected payload shapes
      (invariant d),
    * hallucination flag-rate is 0 (invariant e).

    The dual-judge path (invariant f) is deliberately not wired — see the
    module docstring for why.
    """
    linear = _mock_linear()
    slack = _mock_slack()
    github = _mock_github()
    guard = HallucinationGuard()
    merkle = MerkleLog(session)

    # ---- Reconcile ---------------------------------------------------------
    result = reconcile_week(
        WEEK_DIR,
        session,
        merkle_log=merkle,
        hallucination_guard=guard,
    )

    # Invariant (a): reconciliation result is clean.
    assert isinstance(result, ReconcilerResult)
    assert result.epic_count == 30
    assert result.pass_count == 30
    assert result.passk == pytest.approx(1.0)
    assert result.merkle_chain_intact is True
    assert result.merkle_row_count == 30

    # Invariant (e): every cited artifact resolved.
    assert result.hallucination_flag_rate == pytest.approx(0.0)

    # The drifted-epic count must agree with the emitter's drift heuristic.
    # Phase 1's stub drafter ships drift summaries that do NOT carry the
    # canonical creep/shrink/decision-missed markers (those land with the
    # real-LLM drafter in P1-5), so this is expected to be the empty list
    # against the week_0001 corpus. The invariant being enforced is "the
    # Linear comment-count matches the drift heuristic's verdict" — not
    # "the corpus contains drifted epics".
    drifted = _drifted_epic_ids(result)

    # ---- Emit --------------------------------------------------------------
    out = emit_outputs(
        result,
        session,
        linear=linear,
        slack=slack,
        github=github,
        vp_eng_user_id="U_VPENG",
        github_repo_for_pr="acme/receipts",
        dry_run=False,
    )

    # Invariant (b): emitter result is populated on every channel.
    assert isinstance(out, EmitterResult)
    assert out.dry_run is False
    assert out.markdown_body, "markdown_body must not be empty"
    assert out.markdown_body.startswith("#"), "markdown_body must begin with a heading"
    assert "week_0001" in out.markdown_body
    assert len(out.linear_comment_ids) == len(drifted)
    assert out.linear_comment_ids == [f"linear-comment-{ext_id}" for ext_id in drifted]
    assert out.slack_dm_ts == "1700000000.000999"
    assert out.github_pr_url == "https://github.com/acme/receipts/pull/9001"

    # Invariant (d): connector write methods called with expected payloads.
    assert linear.add_comment.call_count == len(drifted)
    posted_epic_ids = sorted(
        call.kwargs.get("epic_external_id") or call.args[0]
        for call in linear.add_comment.call_args_list
    )
    assert posted_epic_ids == drifted

    slack.send_dm.assert_called_once()
    slack_call = slack.send_dm.call_args
    slack_user = slack_call.kwargs.get("user_id") or slack_call.args[0]
    slack_blocks = slack_call.kwargs.get("blocks") or slack_call.args[1]
    assert slack_user == "U_VPENG"
    assert isinstance(slack_blocks, list) and slack_blocks
    assert slack_blocks[0]["type"] == "header"

    github.create_pull_request.assert_called_once()
    gh_call = github.create_pull_request.call_args
    gh_kwargs = {**gh_call.kwargs}
    gh_args = gh_call.args
    repo = gh_kwargs.get("repo") if "repo" in gh_kwargs else gh_args[0]
    title = gh_kwargs.get("title") if "title" in gh_kwargs else gh_args[1]
    body = gh_kwargs.get("body") if "body" in gh_kwargs else gh_args[2]
    base = gh_kwargs.get("base") if "base" in gh_kwargs else gh_args[3]
    head = gh_kwargs.get("head") if "head" in gh_kwargs else gh_args[4]
    assert repo == "acme/receipts"
    assert title == "receipts: week week_0001 reconciliation"
    assert body == out.markdown_body
    assert base == "main"
    assert head == "receipts/week_0001"


def test_phase1_byte_stable_markdown_across_runs(tmp_path: Path) -> None:
    """Two fresh ledgers + two reconcile/emit passes must produce identical Markdown.

    Determinism is the contract the VP-Eng digest leans on — the same week
    fixture must always render the same Markdown so review of week-N looks
    indistinguishable across reruns + git replays. Each run gets its own
    SQLite file so primary-key allocation is independent (this is the
    harder of the two determinism tests).
    """

    def _one_run(run_idx: int) -> str:
        cfg = Config(str(ALEMBIC_INI))
        url = f"sqlite:///{tmp_path / f'byte_stable_{run_idx}.db'}"
        cfg.set_main_option("sqlalchemy.url", url)
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        command.upgrade(cfg, "head")
        engine = create_engine(url)
        SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        sess = SessionLocal()
        try:
            result = reconcile_week(WEEK_DIR, sess)
            out = emit_outputs(result, sess, dry_run=True)
            return out.markdown_body
        finally:
            sess.close()
            engine.dispose()

    md_first = _one_run(1)
    md_second = _one_run(2)

    assert md_first == md_second, "markdown_body must be byte-stable across runs"


def test_phase1_dry_run_no_writeback(session: Session) -> None:
    """``dry_run=True`` must short-circuit every connector write.

    The CLI advertises ``--dry-run`` as the contract for "preview a week
    without touching Linear / Slack / GitHub". This test enforces that
    contract end-to-end: a full reconcile + emit pass with every connector
    mocked must leave every write method untouched.
    """
    linear = _mock_linear()
    slack = _mock_slack()
    github = _mock_github()

    result = reconcile_week(WEEK_DIR, session)
    out = emit_outputs(
        result,
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
    assert out.dry_run is True
    assert out.markdown_body  # markdown is still built on the dry path
    assert out.linear_comment_ids == []
    assert out.slack_dm_ts is None
    assert out.github_pr_url is None


def test_phase1_cli_subprocess_dry_run() -> None:
    """The published ``python -m receipts.cli.eng`` entrypoint must exit 0 cleanly.

    This is the operator-facing seam: the same command an SRE will type at
    the terminal. Strips the four optional token env vars so the CLI
    doesn't accidentally try to construct a real connector on a developer
    workstation that has ``LINEAR_API_KEY`` exported.
    """
    env = os.environ.copy()
    for key in ("LINEAR_API_KEY", "SLACK_BOT_TOKEN", "GITHUB_TOKEN", "GRANOLA_API_KEY"):
        env.pop(key, None)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "receipts.cli.eng",
            "run",
            "--week-fixture",
            str(WEEK_DIR),
            "--dry-run",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
    stdout = completed.stdout
    # Canonical summary fields the operator-facing one-screen output must
    # name. These are the fields downstream SRE tooling greps for.
    for field in ("week_id", "epic_count", "pass_count", "passk", "merkle_chain_intact"):
        assert field in stdout, f"expected {field!r} in CLI stdout, got: {stdout!r}"
    assert "week_0001" in stdout
