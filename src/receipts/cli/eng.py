"""P1-8: ``receipts-eng`` CLI entrypoint.

The CLI is the operator-facing wrapper over the engineering-vertical
weekly cycle. One subcommand — ``run`` — orchestrates ``reconcile_week``
+ ``emit_outputs`` against a fixture directory and prints a one-screen
summary of the canonical metrics (pass^k, κ, hallucination flag-rate,
Merkle status) plus the head of the Markdown digest.

Exit codes
----------
* ``0`` — every gate passed and the summary was emitted.
* ``1`` — at least one gate tripped (passk below threshold, kappa below
  threshold when measured, or Merkle chain broken).
* ``2`` — argparse / usage error (missing required arg, unknown flag,
  ``--week-fixture`` points at a path that isn't a directory).

Connector wiring
----------------
Connectors are constructed lazily — only when the matching token env var
is non-empty in the process environment. This means a bare ``--dry-run``
on a developer workstation with no real Linear/Slack/GitHub credentials
will still exit cleanly: the emitter just sees ``None`` for the missing
channels and skips them.

Database
--------
The CLI builds a fresh SQLAlchemy engine + session via the L1 helpers
(``make_engine`` + ``make_session_factory``) and runs ``alembic upgrade
head`` against it before invoking the reconciler. For the default
``sqlite:///:memory:`` URL we transparently swap to a temp-file SQLite
so alembic and the session share the same database (alembic spins its
own engine inside ``env.py``; two independent ``:memory:`` engines
would not share state).
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from alembic.config import Config

from alembic import command
from receipts.eng import emit_outputs, reconcile_week
from receipts.ledger.db import make_engine, make_session_factory
from receipts.ledger.merkle import MerkleLog

__all__ = ["main"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: How many characters of the Markdown digest to surface in the stdout
#: summary block. The full digest is built in-process but the CLI is a
#: one-screen overview, not a paginator.
_MARKDOWN_PREVIEW_CHARS = 800


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


class _NonExitingError(Exception):
    """Raised by the custom argparse error path so main() can map to exit 2."""


class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that raises instead of calling ``sys.exit`` on error.

    argparse's default ``error`` writes to stderr and calls ``sys.exit(2)``;
    that's actually the contract we want, but raising lets ``main`` route
    the exit through a single return statement so it stays testable.
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        raise _NonExitingError(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="receipts-eng",
        description="Engineering Receipts weekly reconciler.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Reconcile one week's fixture corpus.")
    run.add_argument(
        "--week-fixture",
        required=True,
        type=Path,
        help="Path to the week fixture directory (e.g. fixtures/eng/week_0001).",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Build outputs but skip every external connector write.",
    )
    run.add_argument(
        "--linear-token-env",
        default="LINEAR_API_KEY",
        help="Env var that holds the Linear API key (default: LINEAR_API_KEY).",
    )
    run.add_argument(
        "--slack-token-env",
        default="SLACK_BOT_TOKEN",
        help="Env var that holds the Slack bot token (default: SLACK_BOT_TOKEN).",
    )
    run.add_argument(
        "--github-token-env",
        default="GITHUB_TOKEN",
        help="Env var that holds the GitHub token (default: GITHUB_TOKEN).",
    )
    run.add_argument(
        "--granola-token-env",
        default="GRANOLA_API_KEY",
        help="Env var that holds the Granola API key (default: GRANOLA_API_KEY).",
    )
    run.add_argument(
        "--vp-eng-slack-user-id",
        default=None,
        help="Slack user id of the VP Eng to DM (optional).",
    )
    run.add_argument(
        "--github-repo",
        default=None,
        help="owner/repo for the digest PR (optional).",
    )
    run.add_argument(
        "--db-url",
        default="sqlite:///:memory:",
        help="SQLAlchemy URL for the ledger (default: sqlite:///:memory:).",
    )
    run.add_argument(
        "--passk-threshold",
        default=0.95,
        type=float,
        help="Minimum pass^1 the reconciler must report (default: 0.95).",
    )
    run.add_argument(
        "--kappa-threshold",
        default=0.40,
        type=float,
        help="Minimum κ when a dual judge is wired (default: 0.40).",
    )
    return parser


# ---------------------------------------------------------------------------
# Connector helpers
# ---------------------------------------------------------------------------


def _build_linear(env_var: str) -> object | None:
    """Construct a LinearConnector iff the env var is set, else None."""
    token = os.environ.get(env_var)
    if not token:
        return None
    from receipts.connectors import LinearConnector

    return LinearConnector(api_key=token)


def _build_slack(env_var: str) -> object | None:
    token = os.environ.get(env_var)
    if not token:
        return None
    from receipts.connectors import SlackConnector

    return SlackConnector(bot_token=token)


def _build_github(env_var: str) -> object | None:
    token = os.environ.get(env_var)
    if not token:
        return None
    from receipts.connectors import GitHubConnector

    return GitHubConnector(token=token)


def _build_granola(env_var: str) -> object | None:
    """Granola is wired for symmetry but the emitter does not consume it.

    Granola data lands in the L1 ledger via the fixture JSONL; the
    connector is only constructed so the CLI surface matches the four
    documented env-var flags. We accept and discard the instance.
    """
    token = os.environ.get(env_var)
    if not token:
        return None
    from receipts.connectors import GranolaConnector

    return GranolaConnector(api_key=token)


# ---------------------------------------------------------------------------
# Database bootstrap
# ---------------------------------------------------------------------------


def _resolve_db_url(db_url: str, tmpdir: str) -> str:
    """Translate ``:memory:`` into a tmpdir file so alembic + session agree.

    Alembic's ``env.py`` builds its own engine from the URL. For sqlite
    ``:memory:`` two distinct engines mean two distinct empty databases;
    the migration would land in one and the reconciler would write into
    the other. Backing memory with a temp file keeps both pointing at
    the same SQLite file for the duration of one CLI invocation.
    """
    if db_url == "sqlite:///:memory:":
        return f"sqlite:///{Path(tmpdir) / 'receipts-eng-cli.db'}"
    return db_url


def _alembic_upgrade(db_url: str) -> None:
    """Run ``alembic upgrade head`` against ``db_url``.

    The CLI walks up from this module to the repo root so users can run
    ``python -m receipts.cli.eng`` from any cwd and still find the
    canonical ``alembic.ini`` + migrations.
    """
    repo_root = Path(__file__).resolve().parents[3]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    command.upgrade(cfg, "head")


# ---------------------------------------------------------------------------
# Summary block
# ---------------------------------------------------------------------------


def _format_summary(
    *,
    week_id: str,
    epic_count: int,
    pass_count: int,
    passk: float,
    kappa: float | None,
    hallucination_rate: float | None,
    merkle_chain_intact: bool,
    merkle_row_count: int,
    markdown_body: str,
) -> str:
    """Render the canonical one-screen summary."""
    lines: list[str] = []
    lines.append(f"week_id: {week_id}")
    lines.append(f"epic_count: {epic_count}")
    lines.append(f"pass_count: {pass_count}")
    lines.append(f"passk: {passk:.3f}")
    lines.append(f"kappa: {kappa:.3f}" if kappa is not None else "kappa: n/a")
    lines.append(
        f"hallucination_rate: {hallucination_rate:.3f}"
        if hallucination_rate is not None
        else "hallucination_rate: n/a"
    )
    lines.append(f"merkle_chain_intact: {merkle_chain_intact}")
    lines.append(f"merkle_row_count: {merkle_row_count}")
    lines.append("")
    lines.append("--- markdown preview ---")
    lines.append(markdown_body[:_MARKDOWN_PREVIEW_CHARS])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _run(args: argparse.Namespace) -> int:
    """Execute the ``run`` subcommand. Returns the process exit code."""
    week_dir = Path(args.week_fixture)
    if not week_dir.is_dir():
        # Surface as argparse usage error so the harness exits 2.
        sys.stderr.write(
            f"receipts-eng: error: --week-fixture path is not a directory: {week_dir}\n"
        )
        return 2

    with tempfile.TemporaryDirectory(prefix="receipts-eng-") as tmpdir:
        db_url = _resolve_db_url(args.db_url, tmpdir)
        _alembic_upgrade(db_url)

        engine = make_engine(db_url)
        session_factory = make_session_factory(engine)
        with session_factory() as session:
            linear = _build_linear(args.linear_token_env)
            slack = _build_slack(args.slack_token_env)
            github = _build_github(args.github_token_env)
            _build_granola(args.granola_token_env)  # symmetry only

            merkle = MerkleLog(session)
            result = reconcile_week(
                week_dir,
                session,
                drafter_judge=None,
                dual_judge=None,
                hallucination_guard=None,
                merkle_log=merkle,
            )

            emitter_out = emit_outputs(
                result,
                session,
                linear=linear,
                slack=slack,
                github=github,
                vp_eng_user_id=args.vp_eng_slack_user_id,
                github_repo_for_pr=args.github_repo,
                dry_run=args.dry_run,
            )

        engine.dispose()

    summary = _format_summary(
        week_id=result.week_id,
        epic_count=result.epic_count,
        pass_count=result.pass_count,
        passk=result.passk,
        kappa=result.kappa,
        hallucination_rate=result.hallucination_flag_rate,
        merkle_chain_intact=result.merkle_chain_intact,
        merkle_row_count=result.merkle_row_count,
        markdown_body=emitter_out.markdown_body,
    )
    print(summary)

    # ---- Gate evaluation ----------------------------------------------------
    gate_failed = False
    if result.passk < args.passk_threshold:
        gate_failed = True
    if result.kappa is not None and result.kappa < args.kappa_threshold:
        gate_failed = True
    if result.merkle_chain_intact is False:
        gate_failed = True

    return 1 if gate_failed else 0


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv`` and dispatch the requested subcommand.

    Returns the process exit code:

    * ``0`` — clean success,
    * ``1`` — gate failure,
    * ``2`` — argparse / usage error.
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except _NonExitingError:
        return 2

    if args.command == "run":
        return _run(args)

    # Defensive: argparse with ``required=True`` already rejects this path.
    parser.print_help(sys.stderr)  # pragma: no cover
    return 2  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
