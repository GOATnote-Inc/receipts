"""L4 lineage query API tests.

These exercise `LineageQuery`, a thin SQLAlchemy 2.0 traversal facade over the
L1 schema. All cross-references go through the polymorphic `edge` table — there
are no direct FKs between epic/pr/commit/meeting/thread, so every lookup is an
edge join.

Setup pattern mirrors tests/test_ledger_schema.py + tests/test_ledger_merkle.py:
SQLite file in tmpdir, `alembic upgrade head`, then hand-inserted fixture rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from receipts.ledger.models import PR, Commit, Edge, Epic, Meeting, Thread
from receipts.ledger.queries import LineageQuery

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'receipts.db'}"


@pytest.fixture
def upgraded_engine(db_url: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(cfg, "head")
    engine = create_engine(db_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session(upgraded_engine) -> Session:
    SessionFactory = sessionmaker(bind=upgraded_engine, expire_on_commit=False)
    with SessionFactory() as s:
        yield s


def _seed_lineage(s: Session) -> dict:
    """Insert a small graph: 2 epics, 3 PRs, 4 commits, 2 meetings, 2 threads.

    Layout:
      EPIC-A
        - PR-1 (with 2 commits: c1, c2)
        - PR-2 (with 1 commit: c3)
        - meeting M-1
        - thread T-1
      EPIC-B
        - PR-3 (with 1 commit: c4)
        - meeting M-2
        - thread T-2

    Returns external-id map for assertions.
    """
    t0 = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)

    epic_a = Epic(
        external_id="LIN-A",
        title="Epic A",
        acceptance_criteria={"must": ["a1"]},
        created_at=t0,
        updated_at=t0,
    )
    epic_b = Epic(
        external_id="LIN-B",
        title="Epic B",
        acceptance_criteria={"must": ["b1"]},
        created_at=t0 + timedelta(days=10),
        updated_at=t0 + timedelta(days=10),
    )
    s.add_all([epic_a, epic_b])
    s.flush()

    pr1 = PR(external_id="gh/foo#1", repo="foo", number=1, title="PR1", summary="")
    pr2 = PR(external_id="gh/foo#2", repo="foo", number=2, title="PR2", summary="")
    pr3 = PR(external_id="gh/foo#3", repo="foo", number=3, title="PR3", summary="")
    s.add_all([pr1, pr2, pr3])
    s.flush()

    c1 = Commit(sha="a" * 40, repo="foo", author="dev", message="c1", committed_at=t0)
    c2 = Commit(sha="b" * 40, repo="foo", author="dev", message="c2", committed_at=t0)
    c3 = Commit(sha="c" * 40, repo="foo", author="dev", message="c3", committed_at=t0)
    c4 = Commit(sha="d" * 40, repo="foo", author="dev", message="c4", committed_at=t0)
    s.add_all([c1, c2, c3, c4])
    s.flush()

    m1 = Meeting(external_id="zoom/M-1", title="Kickoff A", started_at=t0)
    m2 = Meeting(external_id="zoom/M-2", title="Kickoff B", started_at=t0)
    s.add_all([m1, m2])
    s.flush()

    th1 = Thread(external_id="slack/T-1", channel="#a", summary="", last_message_at=t0)
    th2 = Thread(external_id="slack/T-2", channel="#b", summary="", last_message_at=t0)
    s.add_all([th1, th2])
    s.flush()

    edges = [
        Edge(
            src_kind="epic", src_id=epic_a.id, dst_kind="pr", dst_id=pr1.id, relation="implements"
        ),
        Edge(
            src_kind="epic", src_id=epic_a.id, dst_kind="pr", dst_id=pr2.id, relation="implements"
        ),
        Edge(
            src_kind="epic", src_id=epic_b.id, dst_kind="pr", dst_id=pr3.id, relation="implements"
        ),
        Edge(src_kind="pr", src_id=pr1.id, dst_kind="commit", dst_id=c1.id, relation="contains"),
        Edge(src_kind="pr", src_id=pr1.id, dst_kind="commit", dst_id=c2.id, relation="contains"),
        Edge(src_kind="pr", src_id=pr2.id, dst_kind="commit", dst_id=c3.id, relation="contains"),
        Edge(src_kind="pr", src_id=pr3.id, dst_kind="commit", dst_id=c4.id, relation="contains"),
        Edge(
            src_kind="meeting",
            src_id=m1.id,
            dst_kind="epic",
            dst_id=epic_a.id,
            relation="discusses",
        ),
        Edge(
            src_kind="meeting",
            src_id=m2.id,
            dst_kind="epic",
            dst_id=epic_b.id,
            relation="discusses",
        ),
        Edge(
            src_kind="thread",
            src_id=th1.id,
            dst_kind="epic",
            dst_id=epic_a.id,
            relation="discusses",
        ),
        Edge(
            src_kind="thread",
            src_id=th2.id,
            dst_kind="epic",
            dst_id=epic_b.id,
            relation="discusses",
        ),
    ]
    s.add_all(edges)
    s.commit()

    return {
        "epic_a": epic_a,
        "epic_b": epic_b,
        "pr1": pr1,
        "pr2": pr2,
        "pr3": pr3,
        "c1": c1,
        "c2": c2,
        "c3": c3,
        "c4": c4,
        "m1": m1,
        "m2": m2,
        "th1": th1,
        "th2": th2,
    }


def test_prs_for_epic_returns_expected_set(session: Session) -> None:
    nodes = _seed_lineage(session)
    q = LineageQuery(session)

    a_prs = {p.external_id for p in q.prs_for_epic("LIN-A")}
    assert a_prs == {"gh/foo#1", "gh/foo#2"}

    b_prs = {p.external_id for p in q.prs_for_epic("LIN-B")}
    assert b_prs == {"gh/foo#3"}

    # Unknown epic returns empty list.
    assert q.prs_for_epic("LIN-MISSING") == []
    assert nodes["epic_a"].id is not None  # sanity


def test_epics_for_pr_traverses_inbound_edges(session: Session) -> None:
    _seed_lineage(session)
    q = LineageQuery(session)

    epics = q.epics_for_pr("gh/foo#1")
    assert {e.external_id for e in epics} == {"LIN-A"}

    epics_missing = q.epics_for_pr("gh/foo#999")
    assert epics_missing == []


def test_commits_for_pr_returns_only_contained_commits(session: Session) -> None:
    _seed_lineage(session)
    q = LineageQuery(session)

    pr1_commits = {c.sha for c in q.commits_for_pr("gh/foo#1")}
    assert pr1_commits == {"a" * 40, "b" * 40}

    pr2_commits = {c.sha for c in q.commits_for_pr("gh/foo#2")}
    assert pr2_commits == {"c" * 40}

    pr3_commits = {c.sha for c in q.commits_for_pr("gh/foo#3")}
    assert pr3_commits == {"d" * 40}


def test_meetings_for_epic_and_threads_for_epic(session: Session) -> None:
    _seed_lineage(session)
    q = LineageQuery(session)

    a_meetings = {m.external_id for m in q.meetings_for_epic("LIN-A")}
    a_threads = {t.external_id for t in q.threads_for_epic("LIN-A")}
    assert a_meetings == {"zoom/M-1"}
    assert a_threads == {"slack/T-1"}

    b_meetings = {m.external_id for m in q.meetings_for_epic("LIN-B")}
    b_threads = {t.external_id for t in q.threads_for_epic("LIN-B")}
    assert b_meetings == {"zoom/M-2"}
    assert b_threads == {"slack/T-2"}


def test_epics_in_window_filters_by_created_at(session: Session) -> None:
    _seed_lineage(session)
    q = LineageQuery(session)

    # Window covering only epic_a (created Jan 1, 2026); epic_b is Jan 11.
    start = datetime(2025, 12, 1)
    end = datetime(2026, 1, 5)
    in_window = q.epics_in_window(start, end)
    assert [e.external_id for e in in_window] == ["LIN-A"]

    # Wider window catches both.
    in_window_all = q.epics_in_window(datetime(2025, 1, 1), datetime(2026, 12, 31))
    assert {e.external_id for e in in_window_all} == {"LIN-A", "LIN-B"}

    # Empty window catches none.
    none = q.epics_in_window(datetime(2027, 1, 1), datetime(2027, 12, 31))
    assert none == []


def test_lineage_graph_returns_citation_complete_payload(session: Session) -> None:
    _seed_lineage(session)
    q = LineageQuery(session)

    graph = q.lineage_graph("LIN-A")
    assert graph["epic"].external_id == "LIN-A"
    assert {p.external_id for p in graph["prs"]} == {"gh/foo#1", "gh/foo#2"}
    assert {c.sha for c in graph["commits"]} == {"a" * 40, "b" * 40, "c" * 40}
    assert {m.external_id for m in graph["meetings"]} == {"zoom/M-1"}
    assert {t.external_id for t in graph["threads"]} == {"slack/T-1"}


def test_lineage_graph_missing_epic_returns_none_payload(session: Session) -> None:
    _seed_lineage(session)
    q = LineageQuery(session)

    graph = q.lineage_graph("LIN-DOES-NOT-EXIST")
    assert graph["epic"] is None
    assert graph["prs"] == []
    assert graph["commits"] == []
    assert graph["meetings"] == []
    assert graph["threads"] == []


def test_query_ignores_edges_with_wrong_relation(session: Session) -> None:
    """Polymorphic edges must filter by relation, not just kind."""
    nodes = _seed_lineage(session)

    # Inject a spurious 'mentions' edge from epic_a -> pr3 (which belongs to epic_b).
    # `prs_for_epic` should still return PR-1, PR-2 only since it filters
    # on relation='implements'.
    session.add(
        Edge(
            src_kind="epic",
            src_id=nodes["epic_a"].id,
            dst_kind="pr",
            dst_id=nodes["pr3"].id,
            relation="mentions",
        )
    )
    session.commit()

    q = LineageQuery(session)
    a_prs = {p.external_id for p in q.prs_for_epic("LIN-A")}
    assert a_prs == {"gh/foo#1", "gh/foo#2"}, (
        "spurious 'mentions' edge must not leak into 'implements' traversal"
    )
