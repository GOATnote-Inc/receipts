"""P2-7: clinical PHI-aware output-emitter tests.

The clinical emitter consumes a :class:`ClinicalReconcilerResult` plus the
live ledger session and an optional :class:`FHIRConnector` and produces:

* a PHI-redacted Markdown CMIO summary (week_id, encounter_count, pass^1, κ,
  hallucination rate, top drift items — encounter IDs only, no free text),
* one FHIR attestation extension write per encounter draft (via the
  injected connector) under a synthetic ``ENC-NNNN → Composition/synth-ENC-NNNN``
  mapping.

PHI discipline
--------------
* The emitter signature must NOT carry a Slack handle — Slack is the
  forbidden channel for clinical output (per CLAUDE.md / STATUS.md).
* The Markdown body must scrub anything that looks like a name
  (capitalized two-word sequence), SSN-pattern (``\\d{3}-\\d{2}-\\d{4}``),
  MRN-pattern (``\\d{6,}``), and date-of-birth-pattern
  (``\\d{1,2}/\\d{1,2}/\\d{4}``). Replacements use ``[REDACTED]``.

Test discipline
---------------
* No real network: the FHIR connector is ``MagicMock(spec=FHIRConnector)``.
* No real reconciler run: we hand-build a :class:`ClinicalReconcilerResult`
  with 3-5 fake drafts so the test surface stays pinned on emitter logic.
* The in-memory SQLite DB is upgraded to ``alembic head`` so any markdown
  helpers that touch the session still work, but the emitter must not
  depend on encounter rows existing for the synthetic Composition mapping.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from receipts.clinical import ClinicalEmitterResult, emit_clinical_outputs
from receipts.clinical.reconciler import ClinicalReconcilerResult
from receipts.connectors.fhir import FHIRConnector
from receipts.drafter.models import Citation, EncounterContract

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


# ---------------------------------------------------------------------------
# Fixtures: in-memory ledger + a hand-built ClinicalReconcilerResult
# ---------------------------------------------------------------------------


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'clinical_emitter.db'}"


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


def _contract(external_id: str, drift_summary: str) -> EncounterContract:
    """Build a minimal :class:`EncounterContract` for emitter input."""
    return EncounterContract(
        external_id=external_id,
        acceptance_criteria=[f"{external_id} criterion"],
        safety_criteria=[f"{external_id} safety floor"],
        citations={
            f"{external_id} criterion": [
                Citation(artifact_kind="note", external_id="NOTE-1")
            ],
            f"{external_id} safety floor": [
                Citation(artifact_kind="transcript", external_id="TX-1")
            ],
        },
        drift_summary=drift_summary,
    )


def _result_with_drafts(
    drafts: list[tuple[str, EncounterContract]],
    *,
    passk: float = 0.9,
    kappa: float | None = 0.5,
    hallucination_flag_rate: float | None = 0.03,
    merkle_chain_intact: bool = True,
) -> ClinicalReconcilerResult:
    return ClinicalReconcilerResult(
        week_id="week_0001",
        drafts=drafts,
        encounter_count=len(drafts),
        pass_count=int(round(passk * len(drafts))),
        passk=passk,
        kappa=kappa,
        hallucination_flag_rate=hallucination_flag_rate,
        merkle_chain_intact=merkle_chain_intact,
        merkle_row_count=len(drafts),
    )


@pytest.fixture
def seeded_result() -> ClinicalReconcilerResult:
    """3 encounters: two drifted, one clean."""
    drafts = [
        (
            "ENC-0001",
            _contract("ENC-001", "ENC-0001: scope-creep — extra differential added."),
        ),
        (
            "ENC-0002",
            _contract("ENC-002", "ENC-0002: scope-shrink — workup dropped."),
        ),
        (
            "ENC-0003",
            _contract("ENC-003", "ENC-0003: shipped as scoped — no drift."),
        ),
    ]
    return _result_with_drafts(drafts)


def _mock_fhir() -> MagicMock:
    fhir = MagicMock(spec=FHIRConnector)
    counter = {"n": 0}

    def _writer(composition_id: str, attestation_payload: dict[str, object]) -> str:
        counter["n"] += 1
        return f"version-{counter['n']}"

    fhir.write_attestation_extension.side_effect = _writer
    return fhir


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_emit_dry_run_skips_fhir_calls(
    session: Session, seeded_result: ClinicalReconcilerResult
) -> None:
    """dry_run=True must skip every FHIR write but still produce Markdown."""
    fhir = _mock_fhir()

    out = emit_clinical_outputs(
        seeded_result,
        session,
        fhir=fhir,
        cmio_email="cmio@example.org",
        dry_run=True,
    )

    fhir.write_attestation_extension.assert_not_called()
    assert isinstance(out, ClinicalEmitterResult)
    assert out.dry_run is True
    assert out.markdown_body
    assert out.fhir_attestation_version_ids == []
    assert out.composition_update_count == 0


def test_emit_markdown_contains_summary_metrics(
    session: Session, seeded_result: ClinicalReconcilerResult
) -> None:
    """Summary block must surface week_id, passk, κ, hallucination rate, merkle."""
    out = emit_clinical_outputs(seeded_result, session, dry_run=True)

    md = out.markdown_body
    assert "week_0001" in md
    # passk = 0.9 → look for "0.9" prefix
    assert "0.900" in md or "0.9" in md
    # κ value
    assert "0.5" in md
    # hallucination rate
    assert "0.03" in md
    # merkle integrity tag
    assert "intact" in md.lower() or "merkle" in md.lower()
    # encounter count surfaced
    assert "3" in md
    # CMIO recipient marker present even when unset
    assert "cmio" in md.lower() or "CMIO" in md


def test_emit_fhir_calls_one_per_encounter_when_not_dry_run(
    session: Session,
) -> None:
    """One FHIR attestation write per draft; 30-draft fixture → 30 calls."""
    drafts = [
        (f"ENC-{i:04d}", _contract(f"ENC-{i:03d}", f"ENC-{i:04d}: clean."))
        for i in range(1, 31)
    ]
    result = _result_with_drafts(drafts, passk=1.0, kappa=None, hallucination_flag_rate=None)
    fhir = _mock_fhir()

    out = emit_clinical_outputs(result, session, fhir=fhir, dry_run=False)

    assert fhir.write_attestation_extension.call_count == 30
    # Composition id maps to synth-ENC-NNNN.
    composition_ids = sorted(
        call.kwargs.get("composition_id") or call.args[0]
        for call in fhir.write_attestation_extension.call_args_list
    )
    assert composition_ids[0] == "synth-ENC-0001"
    assert composition_ids[-1] == "synth-ENC-0030"
    assert out.composition_update_count == 30


def test_emit_returns_collected_version_ids(
    session: Session, seeded_result: ClinicalReconcilerResult
) -> None:
    """FHIR-returned version ids flow through to the result list, in order."""
    fhir = _mock_fhir()

    out = emit_clinical_outputs(
        seeded_result,
        session,
        fhir=fhir,
        dry_run=False,
    )

    assert out.fhir_attestation_version_ids == ["version-1", "version-2", "version-3"]
    assert out.composition_update_count == 3


def test_emit_no_slack_handle_in_signature() -> None:
    """The clinical emitter signature must NOT carry any slack-related parameter.

    PHI discipline (STATUS.md / CLAUDE.md): clinical output goes to FHIR
    write-back + Markdown PR + PDF in-place — Slack DMs are the wrong
    channel because they leak PHI surface area.
    """
    sig = inspect.signature(emit_clinical_outputs)
    param_names = list(sig.parameters)
    assert not any(
        "slack" in name.lower() for name in param_names
    ), f"emitter signature must not carry a slack parameter; got {param_names}"


def test_emit_redacts_phi_patterns(session: Session) -> None:
    """Free-text bleed in drift_summary must be replaced with [REDACTED].

    The emitter scrubs SSN (``\\d{3}-\\d{2}-\\d{4}``), MRN (``\\d{6,}``),
    DOB (``\\d{1,2}/\\d{1,2}/\\d{4}``), and name-like sequences
    (two capitalized words in a row).
    """
    drafts = [
        (
            "ENC-0001",
            _contract(
                "ENC-001",
                "ENC-0001: scope-creep — patient John Smith SSN 123-45-6789 "
                "MRN 1234567 DOB 03/14/1965 mentioned.",
            ),
        ),
    ]
    result = _result_with_drafts(drafts, passk=0.0)

    out = emit_clinical_outputs(result, session, dry_run=True)

    md = out.markdown_body
    assert "[REDACTED]" in md
    # SSN absent
    assert "123-45-6789" not in md
    # MRN absent
    assert "1234567" not in md
    # DOB absent
    assert "03/14/1965" not in md
    # Name absent
    assert "John Smith" not in md
    # Encounter id preserved — the emitter only redacts PHI, not the ID.
    assert "ENC-0001" in md
