"""S3 — drafter fixture corpora + golden round-trip tests.

The drafter packages (``spec_drafter`` and ``encounter_contract``) ship with
stub registries that map external_id → canned output. J4 will replace the
stubs with real LLM dispatch. To make regressions visible *now*, S3 freezes
the stubs' outputs as JSON fixtures and asserts the drafter still produces
byte-identical results.

Each fixture is the full round-trip:

  fixtures/drafter/eng/epic_NNN.json
      {"epic": ..., "execution": ..., "expected_revised_spec": ...}

  fixtures/drafter/clinical/encounter_NNN.json
      {"stub": ..., "expected_contract": ...}

If anyone edits the stub builders, the golden tests fail and the author must
regenerate fixtures intentionally via ``scripts/gen_drafter_fixtures.py``.

The tests also re-run the validator against each golden output to catch
regressions in either the stub builder *or* the validator contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from receipts.drafter import (
    EncounterContract,
    EncounterStub,
    Epic,
    Execution,
    RevisedSpec,
    draft_encounter_contract,
    draft_revised_spec,
    validate_encounter_contract,
    validate_revised_spec,
)

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "drafter"
ENG_DIR = FIXTURES_ROOT / "eng"
CLINICAL_DIR = FIXTURES_ROOT / "clinical"

ENG_FIXTURE_IDS = [f"epic_{i:03d}" for i in range(1, 31)]
CLINICAL_FIXTURE_IDS = [f"encounter_{i:03d}" for i in range(1, 31)]


def _load(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Engineering drafter golden tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_id", ENG_FIXTURE_IDS)
def test_eng_fixture_drafter_matches_golden(fixture_id: str) -> None:
    """draft_revised_spec output matches the frozen JSON for every EPIC fixture."""
    payload = _load(ENG_DIR / f"{fixture_id}.json")
    epic = Epic.model_validate(payload["epic"])
    execution = Execution.model_validate(payload["execution"])
    expected = RevisedSpec.model_validate(payload["expected_revised_spec"])

    produced = draft_revised_spec(epic, execution)

    # Round-trip via model_dump so dict ordering and unset/default fields
    # cannot cause spurious diffs.
    assert produced.model_dump() == expected.model_dump()


@pytest.mark.parametrize("fixture_id", ENG_FIXTURE_IDS)
def test_eng_fixture_validator_passes(fixture_id: str) -> None:
    """Each frozen RevisedSpec satisfies validate_revised_spec."""
    payload = _load(ENG_DIR / f"{fixture_id}.json")
    epic = Epic.model_validate(payload["epic"])
    execution = Execution.model_validate(payload["execution"])
    expected = RevisedSpec.model_validate(payload["expected_revised_spec"])

    # Returns None on success; raises ValidationError on contract violations.
    assert validate_revised_spec(expected, epic, execution) is None


# ---------------------------------------------------------------------------
# Clinical encounter-contract golden tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_id", CLINICAL_FIXTURE_IDS)
def test_clinical_fixture_drafter_matches_golden(fixture_id: str) -> None:
    """draft_encounter_contract output matches the frozen JSON for every ENC fixture."""
    payload = _load(CLINICAL_DIR / f"{fixture_id}.json")
    stub = EncounterStub.model_validate(payload["stub"])
    expected = EncounterContract.model_validate(payload["expected_contract"])

    produced = draft_encounter_contract(stub)

    assert produced.model_dump() == expected.model_dump()


@pytest.mark.parametrize("fixture_id", CLINICAL_FIXTURE_IDS)
def test_clinical_fixture_validator_passes(fixture_id: str) -> None:
    """Each frozen EncounterContract satisfies validate_encounter_contract."""
    payload = _load(CLINICAL_DIR / f"{fixture_id}.json")
    stub = EncounterStub.model_validate(payload["stub"])
    expected = EncounterContract.model_validate(payload["expected_contract"])

    assert validate_encounter_contract(expected, stub) is None


# ---------------------------------------------------------------------------
# Corpus invariants — protect against accidental fixture deletion / dupes.
# ---------------------------------------------------------------------------


def test_eng_fixture_corpus_complete() -> None:
    """All 30 EPIC fixtures exist and have a unique external_id."""
    files = sorted(ENG_DIR.glob("epic_*.json"))
    assert [p.name for p in files] == [f"{fid}.json" for fid in ENG_FIXTURE_IDS]
    ids = {_load(p)["epic"]["external_id"] for p in files}
    assert len(ids) == 30


def test_clinical_fixture_corpus_complete() -> None:
    """All 30 ENC fixtures exist and have a unique external_id."""
    files = sorted(CLINICAL_DIR.glob("encounter_*.json"))
    assert [p.name for p in files] == [f"{fid}.json" for fid in CLINICAL_FIXTURE_IDS]
    ids = {_load(p)["stub"]["external_id"] for p in files}
    assert len(ids) == 30
