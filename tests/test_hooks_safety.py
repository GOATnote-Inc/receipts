"""H1: per-project Pre/PostToolUse safety hooks.

Tests for `.claude/hooks/` shell scripts that gate Bash tool invocations:
- block-external-writeback.sh — blocks unapproved writes to Linear/GitHub/Slack
- block-phi-export.sh — blocks PHI-bearing exports without an audit reason
- stamp-judge-call.sh — appends a JSON line every time receipts.judge.l2 runs

Each hook receives Claude Code's PreToolUse/PostToolUse stdin payload:
    {"tool_name": "Bash", "tool_input": {"command": "<cmd>"}}

PreToolUse hooks: exit 2 == BLOCK, exit 0 == allow.
PostToolUse hooks: exit 0 always (stamping is non-blocking).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / ".claude" / "hooks"
WRITEBACK_HOOK = HOOKS_DIR / "block-external-writeback.sh"
PHI_HOOK = HOOKS_DIR / "block-phi-export.sh"
STAMP_HOOK = HOOKS_DIR / "stamp-judge-call.sh"
JUDGE_LOG = HOOKS_DIR / "judge_call_log.jsonl"


def _run_hook(
    script: Path,
    command: str,
    env_overrides: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    env = os.environ.copy()
    # Strip any pre-existing approval/reason envs so each test starts clean.
    env.pop("RECEIPTS_APPROVAL_TOKEN", None)
    env.pop("RECEIPTS_PHI_REASON", None)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["bash", str(script)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(cwd) if cwd else str(REPO_ROOT),
        check=False,
    )


# -- block-external-writeback ------------------------------------------------


def test_writeback_blocks_github_curl_without_token() -> None:
    proc = _run_hook(
        WRITEBACK_HOOK,
        'curl -X POST https://api.github.com/repos/foo/bar/issues -d \'{"title":"x"}\'',
    )
    assert proc.returncode == 2, proc.stderr
    assert "BLOCKED" in proc.stderr


def test_writeback_blocks_linear_curl_without_token() -> None:
    proc = _run_hook(
        WRITEBACK_HOOK,
        "curl -X POST https://api.linear.app/graphql -d 'mutation{...}'",
    )
    assert proc.returncode == 2
    assert "BLOCKED" in proc.stderr


def test_writeback_blocks_slack_webhook_without_token() -> None:
    proc = _run_hook(
        WRITEBACK_HOOK,
        "curl -X POST https://hooks.slack.com/services/T0/B0/abc -d 'payload'",
    )
    assert proc.returncode == 2
    assert "BLOCKED" in proc.stderr


def test_writeback_blocks_gh_cli_without_token() -> None:
    proc = _run_hook(
        WRITEBACK_HOOK,
        "gh api repos/foo/bar/issues -X POST -f title=x  # uses api.github.com",
    )
    assert proc.returncode == 2
    assert "BLOCKED" in proc.stderr


def test_writeback_allows_with_approval_token() -> None:
    proc = _run_hook(
        WRITEBACK_HOOK,
        "curl -X POST https://api.github.com/repos/foo/bar/issues",
        env_overrides={"RECEIPTS_APPROVAL_TOKEN": "RECEIPTS_HOOK_ALLOW"},
    )
    assert proc.returncode == 0, proc.stderr


def test_writeback_rejects_wrong_approval_token() -> None:
    proc = _run_hook(
        WRITEBACK_HOOK,
        "curl -X POST https://api.github.com/repos/foo/bar/issues",
        env_overrides={"RECEIPTS_APPROVAL_TOKEN": "not-the-sentinel"},
    )
    assert proc.returncode == 2
    assert "BLOCKED" in proc.stderr


def test_writeback_allows_benign_curl() -> None:
    proc = _run_hook(
        WRITEBACK_HOOK,
        "curl https://example.com/index.html",
    )
    assert proc.returncode == 0, proc.stderr


def test_writeback_allows_unrelated_command() -> None:
    proc = _run_hook(WRITEBACK_HOOK, "ls -la")
    assert proc.returncode == 0, proc.stderr


# -- block-phi-export --------------------------------------------------------


def test_phi_blocks_tar_clinical_fixtures_without_reason() -> None:
    proc = _run_hook(
        PHI_HOOK,
        "tar -czf out.tgz fixtures/clinical/",
    )
    assert proc.returncode == 2, proc.stderr
    assert "BLOCKED" in proc.stderr


def test_phi_blocks_cp_clinical_src_without_reason() -> None:
    proc = _run_hook(
        PHI_HOOK,
        "cp -r src/receipts/clinical/ /tmp/exfil/",
    )
    assert proc.returncode == 2
    assert "BLOCKED" in proc.stderr


def test_phi_blocks_zip_clinical_without_reason() -> None:
    proc = _run_hook(
        PHI_HOOK,
        "zip -r clinical.zip fixtures/clinical",
    )
    assert proc.returncode == 2
    assert "BLOCKED" in proc.stderr


def test_phi_allows_with_reason() -> None:
    proc = _run_hook(
        PHI_HOOK,
        "tar -czf out.tgz fixtures/clinical/",
        env_overrides={"RECEIPTS_PHI_REASON": "audit-2026-05-10"},
    )
    assert proc.returncode == 0, proc.stderr


def test_phi_rejects_empty_reason() -> None:
    proc = _run_hook(
        PHI_HOOK,
        "tar -czf out.tgz fixtures/clinical/",
        env_overrides={"RECEIPTS_PHI_REASON": ""},
    )
    assert proc.returncode == 2
    assert "BLOCKED" in proc.stderr


def test_phi_allows_unrelated_command() -> None:
    proc = _run_hook(PHI_HOOK, "tar -czf out.tgz src/receipts/judge/")
    assert proc.returncode == 0, proc.stderr


# -- stamp-judge-call (PostToolUse) ------------------------------------------


@pytest.fixture
def clean_judge_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run the stamp hook from a temp cwd; it writes to <cwd>/.claude/hooks/."""
    (tmp_path / ".claude" / "hooks").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path / ".claude" / "hooks" / "judge_call_log.jsonl"


def test_stamp_appends_on_judge_l2_command(clean_judge_log: Path) -> None:
    proc = _run_hook(
        STAMP_HOOK,
        'python -c "import receipts.judge.l2 as j; j.run()"',
        cwd=clean_judge_log.parent.parent.parent,
    )
    assert proc.returncode == 0, proc.stderr
    assert clean_judge_log.exists(), "judge_call_log.jsonl should be created"
    lines = clean_judge_log.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert "ts" in entry
    assert "command" in entry
    assert "receipts.judge.l2" in entry["command"]


def test_stamp_ignores_non_judge_command(clean_judge_log: Path) -> None:
    proc = _run_hook(
        STAMP_HOOK,
        "echo hi",
        cwd=clean_judge_log.parent.parent.parent,
    )
    assert proc.returncode == 0, proc.stderr
    assert not clean_judge_log.exists(), "log must not exist for unrelated commands"


def test_stamp_truncates_long_commands(clean_judge_log: Path) -> None:
    long_cmd = "python -c 'import receipts.judge.l2' && " + ("x" * 500)
    proc = _run_hook(
        STAMP_HOOK,
        long_cmd,
        cwd=clean_judge_log.parent.parent.parent,
    )
    assert proc.returncode == 0
    entry = json.loads(clean_judge_log.read_text().strip().splitlines()[0])
    assert len(entry["command"]) <= 200


def test_stamp_appends_on_multiple_invocations(clean_judge_log: Path) -> None:
    for _ in range(3):
        _run_hook(
            STAMP_HOOK,
            "python -m receipts.judge.l2 --score",
            cwd=clean_judge_log.parent.parent.parent,
        )
    assert clean_judge_log.exists()
    lines = clean_judge_log.read_text().strip().splitlines()
    assert len(lines) == 3
    for line in lines:
        json.loads(line)  # each line is valid JSON


def test_stamp_always_exits_zero_even_with_malformed_stdin() -> None:
    """PostToolUse hooks must never block the workflow."""
    proc = subprocess.run(
        ["bash", str(STAMP_HOOK)],
        input="not json at all",
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0


# -- settings.json wiring ----------------------------------------------------


def test_settings_json_wires_all_three_hooks() -> None:
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text())
    assert settings["permissions"]["defaultMode"] == "auto"
    hooks = settings["hooks"]
    pre_commands = [
        h["command"]
        for matcher in hooks["PreToolUse"]
        if matcher["matcher"] == "Bash"
        for h in matcher["hooks"]
    ]
    post_commands = [
        h["command"]
        for matcher in hooks["PostToolUse"]
        if matcher["matcher"] == "Bash"
        for h in matcher["hooks"]
    ]
    assert ".claude/hooks/block-external-writeback.sh" in pre_commands
    assert ".claude/hooks/block-phi-export.sh" in pre_commands
    assert ".claude/hooks/stamp-judge-call.sh" in post_commands
