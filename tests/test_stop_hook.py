"""H2: per-project Stop hook gating pytest + pass^k + κ.

The Stop hook (``.claude/hooks/stop-regression.sh``) fires when the agent's
session ends and gates exit on three substrate checks (CLAUDE.md "Stop hook
gates"):

1. ``make test`` exits 0 (or 5 == no-tests-collected, treated as no-op).
2. If ``fixtures/regression/passk_results.jsonl`` exists, pass^5 >= 0.95.
3. If ``fixtures/regression/kappa_pairs.jsonl`` exists, kappa >= 0.40.

Any gate fail => exit 2 with a ``STOP-GATE:`` stderr message. Exit 0 only
when all enabled gates pass.

Env overrides used by the script (and exercised by these tests):
- ``RECEIPTS_STOP_HOOK_DISABLE=1`` short-circuits to exit 0.
- ``RECEIPTS_STOP_SKIP_PYTEST=1`` skips the ``make test`` stage so unit
  tests can exercise the gate logic without recursive pytest invocations.
- ``RECEIPTS_PASSK_INPUT`` overrides the pass^k fixture path.
- ``RECEIPTS_KAPPA_INPUT`` overrides the kappa fixture path.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STOP_HOOK = REPO_ROOT / ".claude" / "hooks" / "stop-regression.sh"
FIXTURE_DIR = REPO_ROOT / "fixtures" / "regression"
PASSK_GOOD = FIXTURE_DIR / "passk_results.jsonl"
PASSK_BAD = FIXTURE_DIR / "passk_results_bad.jsonl"
KAPPA_GOOD = FIXTURE_DIR / "kappa_pairs.jsonl"
KAPPA_BAD = FIXTURE_DIR / "kappa_pairs_bad.jsonl"


def _run_stop_hook(
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke the Stop hook with a minimal Claude Code Stop payload on stdin.

    The Stop hook itself ignores stdin content, but real Claude Code always
    sends a JSON object, so we mirror that here for fidelity.
    """
    payload = json.dumps({"hook_event_name": "Stop", "stop_hook_active": False})
    env = os.environ.copy()
    # Clear any caller-provided overrides so tests start clean.
    for key in (
        "RECEIPTS_STOP_HOOK_DISABLE",
        "RECEIPTS_STOP_SKIP_PYTEST",
        "RECEIPTS_PASSK_INPUT",
        "RECEIPTS_KAPPA_INPUT",
    ):
        env.pop(key, None)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["bash", str(STOP_HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        check=False,
    )


# -- fixture / artifact sanity ---------------------------------------------


def test_stop_hook_executable() -> None:
    """The Stop hook must be executable so settings.json wiring resolves."""
    assert STOP_HOOK.exists(), f"missing: {STOP_HOOK}"
    mode = STOP_HOOK.stat().st_mode
    assert mode & stat.S_IXUSR, f"stop hook not executable: mode={oct(mode)}"


def test_regression_fixtures_present() -> None:
    """All four regression fixtures must exist so the hook has data to gate on."""
    for path in (PASSK_GOOD, PASSK_BAD, KAPPA_GOOD, KAPPA_BAD):
        assert path.exists(), f"missing regression fixture: {path}"


# -- env-driven short-circuits --------------------------------------------


def test_disable_env_short_circuits() -> None:
    """RECEIPTS_STOP_HOOK_DISABLE=1 must exit 0 with no test/passk/kappa work."""
    proc = _run_stop_hook(env_overrides={"RECEIPTS_STOP_HOOK_DISABLE": "1"})
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    # Sanity: nothing about the gates should have run.
    assert "STOP-GATE" not in proc.stderr


# -- pass^k gate -----------------------------------------------------------


def test_passk_good_fixture_passes() -> None:
    """Good pass^k fixture (25/25 trials pass) yields exit 0."""
    proc = _run_stop_hook(
        env_overrides={
            "RECEIPTS_STOP_SKIP_PYTEST": "1",
            "RECEIPTS_PASSK_INPUT": str(PASSK_GOOD),
            # Disable kappa for isolation.
            "RECEIPTS_KAPPA_INPUT": str(FIXTURE_DIR / "_does_not_exist.jsonl"),
        }
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


def test_passk_bad_fixture_fails() -> None:
    """Pass^k=0.8 (below 0.95) must exit 2 with STOP-GATE message."""
    proc = _run_stop_hook(
        env_overrides={
            "RECEIPTS_STOP_SKIP_PYTEST": "1",
            "RECEIPTS_PASSK_INPUT": str(PASSK_BAD),
            "RECEIPTS_KAPPA_INPUT": str(FIXTURE_DIR / "_does_not_exist.jsonl"),
        }
    )
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert "STOP-GATE" in proc.stderr
    assert "pass^k" in proc.stderr.lower() or "passk" in proc.stderr.lower()


def test_passk_missing_fixture_is_a_noop() -> None:
    """Missing pass^k fixture path skips the gate (exit 0)."""
    proc = _run_stop_hook(
        env_overrides={
            "RECEIPTS_STOP_SKIP_PYTEST": "1",
            "RECEIPTS_PASSK_INPUT": str(FIXTURE_DIR / "_does_not_exist.jsonl"),
            "RECEIPTS_KAPPA_INPUT": str(FIXTURE_DIR / "_does_not_exist.jsonl"),
        }
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


# -- kappa gate ------------------------------------------------------------


def test_kappa_good_fixture_passes() -> None:
    """Perfect-agreement kappa fixture (κ=1.0) yields exit 0."""
    proc = _run_stop_hook(
        env_overrides={
            "RECEIPTS_STOP_SKIP_PYTEST": "1",
            "RECEIPTS_PASSK_INPUT": str(FIXTURE_DIR / "_does_not_exist.jsonl"),
            "RECEIPTS_KAPPA_INPUT": str(KAPPA_GOOD),
        }
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


def test_kappa_bad_fixture_fails() -> None:
    """Kappa near 0 (below 0.40) must exit 2 with STOP-GATE message."""
    proc = _run_stop_hook(
        env_overrides={
            "RECEIPTS_STOP_SKIP_PYTEST": "1",
            "RECEIPTS_PASSK_INPUT": str(FIXTURE_DIR / "_does_not_exist.jsonl"),
            "RECEIPTS_KAPPA_INPUT": str(KAPPA_BAD),
        }
    )
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert "STOP-GATE" in proc.stderr
    assert "kappa" in proc.stderr.lower()


# -- combined gates --------------------------------------------------------


def test_all_good_fixtures_pass_combined() -> None:
    """Both gates passing simultaneously yields exit 0."""
    proc = _run_stop_hook(
        env_overrides={
            "RECEIPTS_STOP_SKIP_PYTEST": "1",
            "RECEIPTS_PASSK_INPUT": str(PASSK_GOOD),
            "RECEIPTS_KAPPA_INPUT": str(KAPPA_GOOD),
        }
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


# -- settings.json wiring --------------------------------------------------


def test_settings_json_wires_stop_hook() -> None:
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())
    stop_entries = settings["hooks"].get("Stop", [])
    commands = [h["command"] for entry in stop_entries for h in entry.get("hooks", [])]
    assert ".claude/hooks/stop-regression.sh" in commands

    # And the H1 hooks must still be wired (regression check).
    pre = [
        h["command"]
        for m in settings["hooks"]["PreToolUse"]
        if m["matcher"] == "Bash"
        for h in m["hooks"]
    ]
    post = [
        h["command"]
        for m in settings["hooks"]["PostToolUse"]
        if m["matcher"] == "Bash"
        for h in m["hooks"]
    ]
    assert ".claude/hooks/block-external-writeback.sh" in pre
    assert ".claude/hooks/block-phi-export.sh" in pre
    assert ".claude/hooks/stamp-judge-call.sh" in post
