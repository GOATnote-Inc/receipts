#!/usr/bin/env python3
"""V2: synthetic engineering-week fixture generator.

Emits JSONL streams (epics / prs / commits / meetings / threads) plus a
ground_truth.json mapping epic external_ids to drift labels. Conforms to the
L1 schema in src/receipts/ledger/models.py (external_id, repo, sha, etc.).

Determinism: every random draw flows through a single ``random.Random(seed)``;
no global ``random`` state is consulted. Two runs with the same seed produce
byte-identical files (verified by test_generator_is_deterministic).

Drift label distribution across the default 30 epics (per spec):
  - 12 "none"
  - 9 "scope-creep"
  - 5 "scope-shrink"
  - 4 "decision-not-reflected"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# -----------------------------------------------------------------------------
# Word banks (avoid faker; keep deterministic and self-contained).
# -----------------------------------------------------------------------------

EPIC_NOUNS = (
    "Checkout",
    "Onboarding",
    "Billing",
    "Search",
    "Dashboard",
    "Notifications",
    "Auth",
    "Profile",
    "Inbox",
    "Reporting",
    "Webhooks",
    "Integrations",
    "Exports",
    "Imports",
    "Audit",
    "Roles",
    "Permissions",
    "Settings",
    "Calendar",
    "Tasks",
    "Pricing",
    "Invoices",
    "Sandbox",
    "Telemetry",
    "Alerts",
    "Compliance",
    "Retention",
    "Tagging",
    "Filtering",
    "Routing",
)

EPIC_VERBS = (
    "Rebuild",
    "Refactor",
    "Migrate",
    "Harden",
    "Streamline",
    "Unify",
    "Optimize",
    "Modernize",
    "Consolidate",
    "Instrument",
    "Tighten",
    "Scale",
    "Stabilize",
)

ACCEPTANCE_TEMPLATES = (
    "users can {action} without manual support intervention",
    "p95 latency under 300 ms for the {area} endpoint",
    "audit log records every {area} mutation with actor + timestamp",
    "feature ships behind {flag} with kill-switch documented",
    "{role} dashboard shows live {area} health within 30 s",
    "rollback procedure validated in staging end-to-end",
    "{area} cardinality cap enforced at 10k entries per tenant",
    "telemetry covers success + error rates for every {area} call",
)

ACTIONS = (
    "self-serve a refund",
    "rotate API keys",
    "export their data",
    "invite teammates",
    "configure SSO",
    "enable 2FA",
    "merge accounts",
    "schedule reports",
)

AREAS = (
    "checkout",
    "onboarding",
    "billing",
    "search",
    "auth",
    "profile",
    "reporting",
    "webhooks",
)

FLAGS = (
    "checkout_v2",
    "onboarding_revamp",
    "billing_split",
    "search_rerank",
    "auth_step_up",
    "profile_unified",
    "reporting_async",
)

ROLES = ("admin", "owner", "operator", "viewer", "billing-manager")

REPOS = (
    "platform",
    "web",
    "mobile",
    "infra",
    "ml-tooling",
    "billing-svc",
)

PR_VERBS = (
    "add",
    "remove",
    "refactor",
    "fix",
    "rename",
    "split",
    "consolidate",
    "harden",
    "instrument",
    "polish",
)

PR_OBJECTS = (
    "metrics emitter",
    "retry policy",
    "rate limiter",
    "feature flag check",
    "input validator",
    "background worker",
    "DB index",
    "cache wrapper",
    "auth middleware",
    "billing webhook",
    "audit hook",
    "config loader",
    "schema migration",
    "error boundary",
    "tenant scope",
)

COMMIT_VERBS = (
    "wire up",
    "tighten",
    "split",
    "rename",
    "drop unused",
    "guard against",
    "extract helper for",
    "add logging around",
    "document",
    "stabilize",
)

COMMIT_OBJECTS = (
    "edge case",
    "race condition",
    "type signature",
    "test helper",
    "fixture loader",
    "retry budget",
    "metric label",
    "feature flag",
    "auth check",
    "cache key",
    "schema bump",
)

AUTHORS = (
    "alice@example.com",
    "bob@example.com",
    "carol@example.com",
    "dave@example.com",
    "erin@example.com",
    "frank@example.com",
    "gina@example.com",
    "hugo@example.com",
)

MEETING_TITLES = (
    "Weekly engineering sync",
    "Architecture review",
    "Sprint planning",
    "Incident retro",
    "Roadmap checkpoint",
    "Cross-team alignment",
    "Eng / product sync",
    "Quality bar review",
)

DECISION_TEMPLATES = (
    "we will defer {area} polish to next quarter",
    "{area} ships behind {flag} regardless of completeness",
    "we drop the {area} celebratory animation from scope",
    "raise the {area} rate-limit cap to 500/min",
    "freeze {area} schema until migration lands",
    "add manual override for {area} for support team",
    "{role} approval required before {area} hits 1% rollout",
)

CHANNELS = (
    "#eng-platform",
    "#eng-web",
    "#eng-infra",
    "#eng-mobile",
    "#eng-billing",
    "#product-eng",
    "#oncall",
    "#sev-1",
    "#design-review",
)

THREAD_SUMMARIES = (
    "debate over which service owns the {area} reconciliation job",
    "support reports {area} is silently dropping retries",
    "design proposal for {area} caching layer (lean toward Redis)",
    "post-incident discussion on {area} error budget burn",
    "feedback on {flag} dogfood rollout",
    "open question: do we lift the {area} rate-limit?",
    "draft RFC: split {area} into read + write paths",
)


# -----------------------------------------------------------------------------
# Helpers.
# -----------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    """Render a UTC datetime to ISO-8601 with explicit offset."""
    return dt.astimezone(UTC).isoformat()


def _drift_label_sequence(rng: random.Random) -> list[str]:
    """Build a deterministic list of 30 drift labels in the spec distribution.

    Labels are then shuffled with the provided RNG so the ordering itself
    contributes randomness while the counts remain exact.
    """
    labels: list[str] = (
        ["none"] * 12 + ["scope-creep"] * 9 + ["scope-shrink"] * 5 + ["decision-not-reflected"] * 4
    )
    assert len(labels) == 30
    rng.shuffle(labels)
    return labels


def _epic_title(rng: random.Random, idx: int) -> str:
    return f"{rng.choice(EPIC_VERBS)} {rng.choice(EPIC_NOUNS)} ({idx + 1:02d})"


def _acceptance_criteria(rng: random.Random, count: int) -> list[str]:
    items: list[str] = []
    for _ in range(count):
        tmpl = rng.choice(ACCEPTANCE_TEMPLATES)
        item = tmpl.format(
            action=rng.choice(ACTIONS),
            area=rng.choice(AREAS),
            flag=rng.choice(FLAGS),
            role=rng.choice(ROLES),
        )
        items.append(item)
    return items


def _sha(rng: random.Random) -> str:
    """40-char hex SHA seeded from the RNG (no real hashing required)."""
    return "".join(rng.choice("0123456789abcdef") for _ in range(40))


def _pr_title(rng: random.Random, epic_idx: int | None) -> str:
    head = f"{rng.choice(PR_VERBS)} {rng.choice(PR_OBJECTS)}"
    if epic_idx is not None:
        return f"{head} for EPIC-{epic_idx + 1:04d}"
    return head


def _pr_summary(rng: random.Random) -> str:
    bullets = []
    for _ in range(rng.randint(2, 4)):
        bullets.append(f"- {rng.choice(COMMIT_VERBS)} {rng.choice(COMMIT_OBJECTS)}")
    return "\n".join(bullets)


def _commit_message(rng: random.Random) -> str:
    return f"{rng.choice(COMMIT_VERBS)} {rng.choice(COMMIT_OBJECTS)}"


def _meeting_decision(rng: random.Random) -> str:
    return rng.choice(DECISION_TEMPLATES).format(
        area=rng.choice(AREAS),
        flag=rng.choice(FLAGS),
        role=rng.choice(ROLES),
    )


def _thread_summary(rng: random.Random) -> str:
    return rng.choice(THREAD_SUMMARIES).format(
        area=rng.choice(AREAS),
        flag=rng.choice(FLAGS),
    )


def _ts_in_week(rng: random.Random, week_start: datetime) -> datetime:
    """Random timestamp within the 7-day window starting at ``week_start``."""
    seconds = rng.randint(0, 7 * 24 * 3600 - 1)
    return week_start + timedelta(seconds=seconds)


# -----------------------------------------------------------------------------
# Per-kind PR counts.
# -----------------------------------------------------------------------------

# Drives both the "real" PR count attached to each epic AND the
# ``expected_pr_count`` field in ground_truth (what the spec implies the team
# scoped). The difference between actual and expected is the drift signal.
KIND_EXPECTED = {
    "none": (3, 5),
    "scope-creep": (3, 5),
    "scope-shrink": (4, 6),
    "decision-not-reflected": (3, 5),
}

KIND_ACTUAL_BONUS = {
    "none": (0, 0),
    "scope-creep": (3, 6),  # team did far more than scoped
    "scope-shrink": (-3, -2),  # team did less than scoped
    "decision-not-reflected": (0, 1),  # roughly on count but ignores a decision
}


# -----------------------------------------------------------------------------
# Core generator.
# -----------------------------------------------------------------------------


def generate(
    out_dir: Path,
    *,
    seed: int = 42,
    epics: int = 30,
    prs: int = 200,
    meetings: int = 30,
    threads: int = 500,
) -> dict[str, int]:
    """Generate the fixture set under ``out_dir`` and return counts."""

    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)

    week_start = datetime(2026, 5, 4, tzinfo=UTC)  # Monday of fixture week.

    # --- Epics + ground truth ------------------------------------------------
    if epics != 30:
        # The fixed 12/9/5/4 distribution only makes sense for 30 epics.
        raise ValueError(f"--epics must be 30 for the spec drift distribution (got {epics})")

    drift_labels = _drift_label_sequence(rng)

    epic_rows: list[dict[str, Any]] = []
    ground_truth: dict[str, dict[str, Any]] = {}
    epic_to_kind: dict[str, str] = {}
    expected_pr_counts: dict[str, int] = {}

    for i in range(epics):
        ext = f"EPIC-{i + 1:04d}"
        created = _ts_in_week(rng, week_start - timedelta(days=14))
        updated = created + timedelta(minutes=rng.randint(60, 7 * 24 * 60))
        acc = _acceptance_criteria(rng, rng.randint(3, 6))
        epic_rows.append(
            {
                "external_id": ext,
                "title": _epic_title(rng, i),
                "acceptance_criteria": acc,
                "created_at": _iso(created),
                "updated_at": _iso(updated),
            }
        )
        kind = drift_labels[i]
        epic_to_kind[ext] = kind
        lo, hi = KIND_EXPECTED[kind]
        expected = rng.randint(lo, hi)
        expected_pr_counts[ext] = expected
        ground_truth[ext] = {
            "drift_kind": kind,
            "expected_pr_count": expected,
            "notes": _ground_truth_note(kind, ext),
        }

    # --- PRs (attached to epics) --------------------------------------------
    # First, compute "actual" PR counts per epic based on drift kind, then top
    # up with unattached PRs to hit the global --prs target.
    pr_rows: list[dict[str, Any]] = []
    pr_counter = 0

    actual_counts: dict[str, int] = {}
    for ext, kind in epic_to_kind.items():
        lo, hi = KIND_ACTUAL_BONUS[kind]
        bonus = rng.randint(lo, hi)
        actual_counts[ext] = max(0, expected_pr_counts[ext] + bonus)

    epic_indices = {ext: i for i, ext in enumerate(epic_to_kind)}

    for ext, n in actual_counts.items():
        for _ in range(n):
            pr_counter += 1
            pr_rows.append(
                _make_pr(
                    rng, pr_counter, epic_ext=ext, epic_idx=epic_indices[ext], week_start=week_start
                )
            )

    # Backfill with unattached PRs (epic_external_id = None) until we hit --prs.
    while pr_counter < prs:
        pr_counter += 1
        pr_rows.append(
            _make_pr(rng, pr_counter, epic_ext=None, epic_idx=None, week_start=week_start)
        )

    # If we already overshot --prs because actual_counts sum > --prs, trim from
    # the tail of unattached PRs only (preserve epic attribution).
    if pr_counter > prs:
        # Sort: keep all attached, drop unattached tail.
        attached = [r for r in pr_rows if r["epic_external_id"] is not None]
        unattached = [r for r in pr_rows if r["epic_external_id"] is None]
        keep_unattached = max(0, prs - len(attached))
        pr_rows = attached + unattached[:keep_unattached]
        # Renumber to keep external_ids dense & deterministic.
        for new_idx, row in enumerate(pr_rows, start=1):
            row["external_id"] = f"PR-{new_idx:04d}"
            row["number"] = new_idx

    # --- Commits (one or more per PR; some loose) ----------------------------
    commit_rows: list[dict[str, Any]] = []
    for pr in pr_rows:
        n_commits = rng.randint(1, 4)
        for _ in range(n_commits):
            sha = _sha(rng)
            committed = _ts_in_week(rng, week_start)
            commit_rows.append(
                {
                    "sha": sha,
                    "repo": pr["repo"],
                    "author": rng.choice(AUTHORS),
                    "message": _commit_message(rng),
                    "committed_at": _iso(committed),
                }
            )
    # A few additional unattached commits (refactors, infra, etc.).
    for _ in range(rng.randint(8, 16)):
        commit_rows.append(
            {
                "sha": _sha(rng),
                "repo": rng.choice(REPOS),
                "author": rng.choice(AUTHORS),
                "message": _commit_message(rng),
                "committed_at": _iso(_ts_in_week(rng, week_start)),
            }
        )

    # --- Meetings ------------------------------------------------------------
    meeting_rows: list[dict[str, Any]] = []
    epic_ids_list = list(epic_to_kind.keys())
    decision_not_reflected_epics = [
        ext for ext, kind in epic_to_kind.items() if kind == "decision-not-reflected"
    ]

    # Pre-assign each "decision-not-reflected" epic to at least one meeting so
    # the drift label has corresponding signal in the corpus.
    pending_decision_epics = list(decision_not_reflected_epics)
    rng.shuffle(pending_decision_epics)

    for i in range(meetings):
        ext = f"MTG-{i + 1:04d}"
        started = _ts_in_week(rng, week_start)
        title = rng.choice(MEETING_TITLES)
        # 1-3 referenced epics, biased to include any pending decision-drift epic.
        ref_count = rng.randint(1, 3)
        refs: list[str] = []
        if pending_decision_epics:
            refs.append(pending_decision_epics.pop())
        while len(refs) < ref_count:
            cand = rng.choice(epic_ids_list)
            if cand not in refs:
                refs.append(cand)
        decisions = [_meeting_decision(rng) for _ in range(rng.randint(1, 3))]
        transcript_ref = _transcript_ref(rng, ext)
        meeting_rows.append(
            {
                "external_id": ext,
                "title": title,
                "started_at": _iso(started),
                "transcript_ref": transcript_ref,
                "decisions": decisions,
                "epic_external_ids": refs,
            }
        )

    # --- Threads -------------------------------------------------------------
    thread_rows: list[dict[str, Any]] = []
    for i in range(threads):
        ext = f"THR-{i + 1:04d}"
        # ~70% of threads reference some epic.
        attach = rng.random() < 0.7
        epic_ext = rng.choice(epic_ids_list) if attach else None
        thread_rows.append(
            {
                "external_id": ext,
                "channel": rng.choice(CHANNELS),
                "summary": _thread_summary(rng),
                "last_message_at": _iso(_ts_in_week(rng, week_start)),
                "epic_external_id": epic_ext,
            }
        )

    # --- Write all artifacts (sorted-key JSON for byte-determinism) ---------
    _write_jsonl(out_dir / "epics.jsonl", epic_rows)
    _write_jsonl(out_dir / "prs.jsonl", pr_rows)
    _write_jsonl(out_dir / "commits.jsonl", commit_rows)
    _write_jsonl(out_dir / "meetings.jsonl", meeting_rows)
    _write_jsonl(out_dir / "threads.jsonl", thread_rows)

    # ground_truth.json: dict keyed by epic external_id, sorted keys.
    gt_path = out_dir / "ground_truth.json"
    gt_path.write_text(
        json.dumps(ground_truth, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return {
        "epics": len(epic_rows),
        "prs": len(pr_rows),
        "commits": len(commit_rows),
        "meetings": len(meeting_rows),
        "threads": len(thread_rows),
    }


def _make_pr(
    rng: random.Random,
    counter: int,
    *,
    epic_ext: str | None,
    epic_idx: int | None,
    week_start: datetime,
) -> dict[str, Any]:
    merged = rng.random() < 0.85
    merged_at = _ts_in_week(rng, week_start) if merged else None
    merged_sha = _sha(rng) if merged else None
    return {
        "external_id": f"PR-{counter:04d}",
        "repo": rng.choice(REPOS),
        "number": counter,
        "merged_sha": merged_sha,
        "title": _pr_title(rng, epic_idx),
        "summary": _pr_summary(rng),
        "merged_at": _iso(merged_at) if merged_at is not None else None,
        "epic_external_id": epic_ext,
    }


def _transcript_ref(rng: random.Random, meeting_ext: str) -> str:
    digest = hashlib.sha256(f"{meeting_ext}-{rng.random()}".encode()).hexdigest()[:16]
    return f"s3://receipts-fixtures/transcripts/{meeting_ext.lower()}-{digest}.json"


def _ground_truth_note(kind: str, ext: str) -> str:
    if kind == "none":
        return f"{ext}: PRs match scoped acceptance criteria — no drift signal."
    if kind == "scope-creep":
        return f"{ext}: team merged additional PRs not covered by the original spec."
    if kind == "scope-shrink":
        return (
            f"{ext}: team shipped fewer PRs than scoped — at least one acceptance "
            "criterion is unaddressed by merged work."
        )
    if kind == "decision-not-reflected":
        return (
            f"{ext}: meeting decision (see linked MTG-* entries) is not reflected "
            "in any merged PR title or summary."
        )
    raise ValueError(f"unknown drift kind: {kind}")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            # sort_keys=True for stable byte output regardless of dict order.
            fh.write(json.dumps(row, sort_keys=True))
            fh.write("\n")


# -----------------------------------------------------------------------------
# CLI.
# -----------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--out", required=True, type=Path, help="Output directory")
    p.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    p.add_argument("--epics", type=int, default=30, help="Number of epics (default: 30)")
    p.add_argument("--prs", type=int, default=200, help="Number of PRs (default: 200)")
    p.add_argument("--meetings", type=int, default=30, help="Number of meetings (default: 30)")
    p.add_argument("--threads", type=int, default=500, help="Number of threads (default: 500)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    counts = generate(
        args.out,
        seed=args.seed,
        epics=args.epics,
        prs=args.prs,
        meetings=args.meetings,
        threads=args.threads,
    )
    summary = ", ".join(f"{k}={v}" for k, v in counts.items())
    print(f"wrote fixture to {args.out} ({summary})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
