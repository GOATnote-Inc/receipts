"""L4: lineage query API.

`LineageQuery` wraps a SQLAlchemy Session and exposes traversal methods over
the L1 temporal graph. All cross-references between Epic / PR / Commit /
Meeting / Thread go through the polymorphic `edge` table — there are no direct
foreign keys, so every lookup is an edge join.

Conventions:
- Edge direction encodes intent. We follow the relation names laid down in the
  V2 fixture and L1 schema spec:
    * epic --[implements]--> pr
    * pr   --[contains]----> commit
    * meeting --[discusses]--> epic
    * thread  --[discusses]--> epic
- Methods take the *external* id of an artifact (Linear key, GH PR URL,
  Slack thread id, etc.) and return ORM rows, never tuples — callers can keep
  using attribute access.
- An unknown external id resolves to an empty list (or a payload of `None` +
  empty lists for `lineage_graph`). No exceptions are raised on lookup miss;
  this matches how the engineering-receipts CLI streams partial graphs.

SQLAlchemy 2.0 `select()` API is used throughout.
"""

from __future__ import annotations

from datetime import datetime
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from receipts.ledger.models import PR, Commit, Edge, Epic, Meeting, Thread


class LineageGraph(TypedDict):
    """Citation-complete lineage payload for a single epic."""

    epic: Epic | None
    prs: list[PR]
    commits: list[Commit]
    meetings: list[Meeting]
    threads: list[Thread]


class LineageQuery:
    """Read-only lineage traversal over the L1 temporal graph."""

    def __init__(self, session: Session) -> None:
        self._s = session

    # ---- internal helpers -------------------------------------------------

    def _epic_id(self, external_id: str) -> int | None:
        return self._s.execute(
            select(Epic.id).where(Epic.external_id == external_id)
        ).scalar_one_or_none()

    def _pr_id(self, external_id: str) -> int | None:
        return self._s.execute(
            select(PR.id).where(PR.external_id == external_id)
        ).scalar_one_or_none()

    # ---- forward traversals (epic -> prs/meetings/threads) ----------------

    def prs_for_epic(self, epic_external_id: str) -> list[PR]:
        """Return PRs reachable via `epic --[implements]--> pr` edges."""
        epic_id = self._epic_id(epic_external_id)
        if epic_id is None:
            return []
        stmt = (
            select(PR)
            .join(Edge, Edge.dst_id == PR.id)
            .where(
                Edge.dst_kind == "pr",
                Edge.src_kind == "epic",
                Edge.src_id == epic_id,
                Edge.relation == "implements",
            )
            .order_by(PR.id.asc())
        )
        return list(self._s.execute(stmt).scalars().all())

    def commits_for_pr(self, pr_external_id: str) -> list[Commit]:
        """Return commits reachable via `pr --[contains]--> commit` edges."""
        pr_id = self._pr_id(pr_external_id)
        if pr_id is None:
            return []
        stmt = (
            select(Commit)
            .join(Edge, Edge.dst_id == Commit.id)
            .where(
                Edge.dst_kind == "commit",
                Edge.src_kind == "pr",
                Edge.src_id == pr_id,
                Edge.relation == "contains",
            )
            .order_by(Commit.id.asc())
        )
        return list(self._s.execute(stmt).scalars().all())

    def meetings_for_epic(self, epic_external_id: str) -> list[Meeting]:
        """Return meetings linked via `meeting --[discusses]--> epic` edges."""
        epic_id = self._epic_id(epic_external_id)
        if epic_id is None:
            return []
        stmt = (
            select(Meeting)
            .join(Edge, Edge.src_id == Meeting.id)
            .where(
                Edge.src_kind == "meeting",
                Edge.dst_kind == "epic",
                Edge.dst_id == epic_id,
                Edge.relation == "discusses",
            )
            .order_by(Meeting.id.asc())
        )
        return list(self._s.execute(stmt).scalars().all())

    def threads_for_epic(self, epic_external_id: str) -> list[Thread]:
        """Return threads linked via `thread --[discusses]--> epic` edges."""
        epic_id = self._epic_id(epic_external_id)
        if epic_id is None:
            return []
        stmt = (
            select(Thread)
            .join(Edge, Edge.src_id == Thread.id)
            .where(
                Edge.src_kind == "thread",
                Edge.dst_kind == "epic",
                Edge.dst_id == epic_id,
                Edge.relation == "discusses",
            )
            .order_by(Thread.id.asc())
        )
        return list(self._s.execute(stmt).scalars().all())

    # ---- inverse traversal (pr -> epics) ----------------------------------

    def epics_for_pr(self, pr_external_id: str) -> list[Epic]:
        """Return epics whose `implements` edges point at this PR."""
        pr_id = self._pr_id(pr_external_id)
        if pr_id is None:
            return []
        stmt = (
            select(Epic)
            .join(Edge, Edge.src_id == Epic.id)
            .where(
                Edge.src_kind == "epic",
                Edge.dst_kind == "pr",
                Edge.dst_id == pr_id,
                Edge.relation == "implements",
            )
            .order_by(Epic.id.asc())
        )
        return list(self._s.execute(stmt).scalars().all())

    # ---- temporal filter --------------------------------------------------

    def epics_in_window(self, start: datetime, end: datetime) -> list[Epic]:
        """Return epics whose `created_at` falls within `[start, end]`."""
        stmt = (
            select(Epic)
            .where(Epic.created_at >= start, Epic.created_at <= end)
            .order_by(Epic.created_at.asc(), Epic.id.asc())
        )
        return list(self._s.execute(stmt).scalars().all())

    # ---- aggregate --------------------------------------------------------

    def lineage_graph(self, epic_external_id: str) -> LineageGraph:
        """Return a citation-complete lineage payload for one epic.

        Shape: `{"epic": Epic | None, "prs": [...], "commits": [...],
        "meetings": [...], "threads": [...]}`. Commits are flattened across
        all PRs belonging to the epic; duplicates are preserved as ORM
        identity-mapped rows (no de-dupe needed since each commit lives on
        a single PR via the `contains` relation).
        """
        epic = self._s.execute(
            select(Epic).where(Epic.external_id == epic_external_id)
        ).scalar_one_or_none()
        if epic is None:
            return LineageGraph(epic=None, prs=[], commits=[], meetings=[], threads=[])

        prs = self.prs_for_epic(epic_external_id)
        commits: list[Commit] = []
        for pr in prs:
            commits.extend(self.commits_for_pr(pr.external_id))
        meetings = self.meetings_for_epic(epic_external_id)
        threads = self.threads_for_epic(epic_external_id)

        return LineageGraph(
            epic=epic,
            prs=prs,
            commits=commits,
            meetings=meetings,
            threads=threads,
        )


__all__ = ["LineageGraph", "LineageQuery"]
