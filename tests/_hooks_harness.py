"""Reusable test harness for Claude Code hook scripts (H3).

Future hook tests should look like:

    from tests._hooks_harness import (
        hook_path, invoke_hook, synthetic_pretooluse, synthetic_stop,
    )

    def test_my_hook_allows_safe_command():
        result = invoke_hook(
            hook_path("block-external-writeback.sh"),
            synthetic_pretooluse("Bash", command="ls -la"),
        )
        assert result.exit_code == 0

This module purposely depends on nothing in `src/receipts/` so it remains
usable from any test file regardless of import order.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class HookResult:
    """Captured stdout/stderr/exit/duration from a hook invocation."""

    exit_code: int
    stdout: str
    stderr: str
    duration_s: float


def invoke_hook(
    script: Path,
    stdin_json: dict,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: float = 5.0,
) -> HookResult:
    """Pipe ``stdin_json`` (as JSON) into ``script`` and return the result.

    The hook is run with ``bash <script>`` to match how Claude Code actually
    invokes hooks (via the script's shebang in production; we standardize
    on bash here so tests don't depend on the script's executable bit).

    A clean environment is built from ``os.environ`` with the hook-relevant
    sentinel envs stripped, then ``env`` overrides are applied on top.
    """
    base_env = os.environ.copy()
    # Strip any sentinels that prior tests / shells may have leaked in so
    # callers get a clean baseline. Callers that want a value must pass it
    # explicitly via ``env``.
    for key in (
        "RECEIPTS_APPROVAL_TOKEN",
        "RECEIPTS_PHI_REASON",
        "RECEIPTS_STOP_HOOK_DISABLE",
        "RECEIPTS_STOP_SKIP_PYTEST",
        "RECEIPTS_PASSK_INPUT",
        "RECEIPTS_KAPPA_INPUT",
    ):
        base_env.pop(key, None)
    if env:
        base_env.update(env)

    payload = json.dumps(stdin_json)
    start = time.perf_counter()
    proc = subprocess.run(
        ["bash", str(script)],
        input=payload,
        capture_output=True,
        text=True,
        env=base_env,
        cwd=str(cwd) if cwd else None,
        timeout=timeout,
        check=False,
    )
    duration = time.perf_counter() - start
    return HookResult(
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        duration_s=duration,
    )


def hook_path(name: str) -> Path:
    """Return ``<repo_top>/.claude/hooks/<name>`` for the current git worktree.

    Uses ``git rev-parse --show-toplevel`` so this works correctly inside
    worktrees (the worktree dir is the toplevel, not the main repo dir).
    """
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return Path(top) / ".claude" / "hooks" / name


def synthetic_pretooluse(
    tool_name: str,
    command: str | None = None,
    file_path: str | None = None,
) -> dict:
    """Build a synthetic PreToolUse payload.

    Mirrors Claude Code's real shape::

        {"session_id": "test", "tool_name": "...", "tool_input": {...}}

    Exactly one of ``command`` / ``file_path`` is typically set, matching
    the tool: Bash uses ``command``; Read/Write/Edit use ``file_path``.
    """
    tool_input: dict = {}
    if command is not None:
        tool_input["command"] = command
    if file_path is not None:
        tool_input["file_path"] = file_path
    return {
        "session_id": "test",
        "tool_name": tool_name,
        "tool_input": tool_input,
    }


def synthetic_posttooluse(
    tool_name: str,
    command: str | None = None,
    file_path: str | None = None,
    success: bool = True,
) -> dict:
    """Build a synthetic PostToolUse payload (PreToolUse + ``tool_response``)."""
    payload = synthetic_pretooluse(tool_name, command=command, file_path=file_path)
    payload["tool_response"] = {"success": success}
    return payload


def synthetic_stop() -> dict:
    """Build a synthetic Stop hook payload (minimal session identifier)."""
    return {
        "session_id": "test",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
    }
