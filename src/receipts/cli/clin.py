"""P2-8: ``receipts-clin`` CLI entrypoint.

The CLI is the operator-facing wrapper over the clinical-vertical weekly
cycle. One subcommand — ``run`` — orchestrates ``reconcile_clinical_week``
+ ``emit_clinical_outputs`` against a clinical fixture directory and
prints a one-screen summary of the canonical metrics (pass^k, κ,
hallucination flag-rate, Merkle status, FHIR attestation count) plus the
head of the PHI-redacted Markdown digest.

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
on a developer workstation with no real Scribe/FHIR credentials will
still exit cleanly: the emitter just sees ``None`` for the FHIR connector
and skips the attestation fan-out. The Scribe connector is symmetrically
constructable for future reads, but the reconciler treats the fixture
JSONL as the truth, so the instance is built and discarded today.

PHI discipline
--------------
The summary block surfaces only the upstream-redacted Markdown preview;
the emitter already strips SSN / MRN / DOB / name-like patterns before
returning ``markdown_body``. No Slack handle is accepted on the CLI
surface — Slack is the forbidden channel for clinical output.

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
from receipts.clinical import emit_clinical_outputs, reconcile_clinical_week
from receipts.ledger.db import make_engine, make_session_factory
from receipts.ledger.merkle import MerkleLog

__all__ = ["main"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: How many characters of the Markdown digest to surface in the stdout
#: summary block. Mirrors the eng CLI for cross-vertical consistency.
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
        prog="receipts-clin",
        description="Clinical Audit Ledger weekly reconciler.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Reconcile one week's clinical fixture corpus.")
    run.add_argument(
        "--week-fixture",
        required=True,
        type=Path,
        help="Path to the clinical week fixture directory (e.g. fixtures/clinical/week_0001).",
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Build outputs but skip every external connector write.",
    )
    run.add_argument(
        "--scribe-token-env",
        default="AMBIENCE_API_KEY",
        help="Env var that holds the Ambience scribe API key (default: AMBIENCE_API_KEY).",
    )
    run.add_argument(
        "--fhir-base-url",
        default=None,
        help="FHIR R4 base URL. When set together with --fhir-token-env, "
        "builds a FHIRConnector for attestation write-back (optional).",
    )
    run.add_argument(
        "--fhir-token-env",
        default="FHIR_BEARER_TOKEN",
        help="Env var that holds the FHIR bearer token (default: FHIR_BEARER_TOKEN).",
    )
    run.add_argument(
        "--cmio-email",
        default=None,
        help="CMIO recipient email address; surfaces in the Markdown summary (optional).",
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


def _build_scribe(env_var: str) -> object | None:
    """Construct an ``AmbienceScribeConnector`` iff the env var is set.

    The reconciler currently treats the fixture JSONL as the source of
    truth, so the connector instance is built for surface symmetry only
    (future P2 sprints will swap fixture reads for live Scribe reads).
    Returning the instance keeps the helper testable; the CLI today
    instantiates and discards it.
    """
    token = os.environ.get(env_var)
    if not token:
        return None
    from receipts.connectors import AmbienceScribeConnector

    return AmbienceScribeConnector(api_key=token)


def _build_fhir(base_url: str | None, env_var: str) -> object | None:
    """Construct a ``FHIRConnector`` iff both base URL + env var are set.

    The emitter only fans out attestation writes when ``fhir`` is non-None
    AND ``dry_run`` is False; the CLI returns None in every other case so
    the emitter short-circuits cleanly.
    """
    if not base_url:
        return None
    token = os.environ.get(env_var)
    if not token:
        return None
    from receipts.connectors import FHIRConnector

    return FHIRConnector(base_url=base_url, bearer_token=token)


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
        return f"sqlite:///{Path(tmpdir) / 'receipts-clin-cli.db'}"
    return db_url


def _alembic_upgrade(db_url: str) -> None:
    """Run ``alembic upgrade head`` against ``db_url``.

    The CLI walks up from this module to the repo root so users can run
    ``python -m receipts.cli.clin`` from any cwd and still find the
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
    encounter_count: int,
    pass_count: int,
    passk: float,
    kappa: float | None,
    hallucination_rate: float | None,
    merkle_chain_intact: bool,
    merkle_row_count: int,
    fhir_attestation_count: int,
    markdown_body: str,
) -> str:
    """Render the canonical one-screen summary for the clinical CLI."""
    lines: list[str] = []
    lines.append(f"week_id: {week_id}")
    lines.append(f"encounter_count: {encounter_count}")
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
    lines.append(f"fhir_attestations: {fhir_attestation_count}")
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
            f"receipts-clin: error: --week-fixture path is not a directory: {week_dir}\n"
        )
        return 2

    with tempfile.TemporaryDirectory(prefix="receipts-clin-") as tmpdir:
        db_url = _resolve_db_url(args.db_url, tmpdir)
        _alembic_upgrade(db_url)

        engine = make_engine(db_url)
        session_factory = make_session_factory(engine)
        with session_factory() as session:
            _build_scribe(args.scribe_token_env)  # symmetry only — fixture-backed today
            fhir = _build_fhir(args.fhir_base_url, args.fhir_token_env)

            merkle = MerkleLog(session)
            result = reconcile_clinical_week(
                week_dir,
                session,
                drafter_judge=None,
                dual_judge=None,
                hallucination_guard=None,
                merkle_log=merkle,
            )

            emitter_out = emit_clinical_outputs(
                result,
                session,
                fhir=fhir,
                cmio_email=args.cmio_email,
                dry_run=args.dry_run,
            )

        engine.dispose()

    summary = _format_summary(
        week_id=result.week_id,
        encounter_count=result.encounter_count,
        pass_count=result.pass_count,
        passk=result.passk,
        kappa=result.kappa,
        hallucination_rate=result.hallucination_flag_rate,
        merkle_chain_intact=result.merkle_chain_intact,
        merkle_row_count=result.merkle_row_count,
        fhir_attestation_count=emitter_out.composition_update_count,
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
