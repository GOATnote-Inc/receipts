"""Shared pytest fixtures for the receipts test suite.

Judge-replay infrastructure stub. Subsequent tasks (J4, J7) flesh this out into
record/replay against fixture files at tests/fixtures/judge_recordings/.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
JUDGE_RECORDINGS_DIR = FIXTURES_DIR / "judge_recordings"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def judge_replay_mode(monkeypatch: pytest.MonkeyPatch) -> str:
    """Default to replay. Set RECEIPTS_JUDGE_MODE=record to capture new fixtures."""
    mode = os.environ.get("RECEIPTS_JUDGE_MODE", "replay")
    monkeypatch.setenv("RECEIPTS_JUDGE_MODE", mode)
    return mode
