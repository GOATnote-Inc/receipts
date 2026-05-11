"""H3: tests for the reusable hook-test harness (``tests/_hooks_harness.py``).

The harness exists so future hook tests are one-liners:

    result = invoke_hook(hook_path("block-external-writeback.sh"),
                        synthetic_pretooluse("Bash", command="..."))
    assert result.exit_code == 0

These tests prove the harness itself works end-to-end against tiny inline
bash scripts and at least one real project hook. Runtime budget: <2s wall
clock for this file alone.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from tests._hooks_harness import (
    HookResult,
    hook_path,
    invoke_hook,
    synthetic_posttooluse,
    synthetic_pretooluse,
    synthetic_stop,
)


# -- core invocation --------------------------------------------------------


def _write_script(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "hook.sh"
    script.write_text("#!/usr/bin/env bash\n" + body + "\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_invoke_hook_captures_exit_code(tmp_path: Path) -> None:
    script = _write_script(tmp_path, "exit 7")
    result = invoke_hook(script, {"session_id": "test"})
    assert isinstance(result, HookResult)
    assert result.exit_code == 7
    assert result.duration_s >= 0.0


def test_invoke_hook_captures_stdout_stderr(tmp_path: Path) -> None:
    script = _write_script(
        tmp_path,
        'printf "hello-out"\nprintf "hello-err" >&2\nexit 0',
    )
    result = invoke_hook(script, {"session_id": "test"})
    assert result.exit_code == 0
    assert result.stdout == "hello-out"
    assert result.stderr == "hello-err"


def test_invoke_hook_passes_stdin_json(tmp_path: Path) -> None:
    # Script reads the JSON payload and echoes back tool_input.command.
    script = _write_script(
        tmp_path,
        'payload=$(cat)\nprintf "%s" "$payload" | '
        "jq -r '.tool_input.command'",
    )
    payload = synthetic_pretooluse("Bash", command="echo round-trip")
    result = invoke_hook(script, payload)
    assert result.exit_code == 0
    assert result.stdout.strip() == "echo round-trip"


def test_invoke_hook_respects_env_override() -> None:
    """The real block-external-writeback hook must allow a curl when the
    sentinel approval token is set via env override."""
    script = hook_path("block-external-writeback.sh")
    payload = synthetic_pretooluse(
        "Bash",
        command="curl -X POST https://api.github.com/repos/foo/bar/issues",
    )
    result = invoke_hook(
        script,
        payload,
        env={"RECEIPTS_APPROVAL_TOKEN": "RECEIPTS_HOOK_ALLOW"},
    )
    assert result.exit_code == 0, result.stderr


def test_invoke_hook_timeout_raises(tmp_path: Path) -> None:
    """Timeout should surface as a subprocess.TimeoutExpired (or wrapped)."""
    script = _write_script(tmp_path, "sleep 5")
    with pytest.raises(Exception) as exc_info:  # noqa: BLE001 - want the type
        invoke_hook(script, {"session_id": "test"}, timeout=0.2)
    # Accept either TimeoutExpired or a wrapped exception that mentions timeout.
    name = type(exc_info.value).__name__.lower()
    assert "timeout" in name or "timeout" in str(exc_info.value).lower()


# -- synthetic payload shapes ----------------------------------------------


def test_synthetic_pretooluse_shape() -> None:
    payload = synthetic_pretooluse("Bash", command="ls -la")
    assert payload["tool_name"] == "Bash"
    assert payload["tool_input"]["command"] == "ls -la"
    assert payload["session_id"] == "test"
    # Round-trip through JSON to prove it's serializable.
    assert json.loads(json.dumps(payload)) == payload


def test_synthetic_pretooluse_file_path_variant() -> None:
    payload = synthetic_pretooluse("Read", file_path="/etc/hosts")
    assert payload["tool_name"] == "Read"
    assert payload["tool_input"]["file_path"] == "/etc/hosts"
    assert "command" not in payload["tool_input"]


def test_synthetic_posttooluse_includes_tool_response() -> None:
    payload = synthetic_posttooluse("Bash", command="echo hi", success=True)
    assert payload["tool_name"] == "Bash"
    assert payload["tool_input"]["command"] == "echo hi"
    assert "tool_response" in payload
    assert payload["tool_response"]["success"] is True


def test_synthetic_stop_minimal_payload() -> None:
    payload = synthetic_stop()
    assert payload["session_id"] == "test"
    assert json.loads(json.dumps(payload)) == payload


# -- hook_path resolution --------------------------------------------------


def test_hook_path_resolves_existing_file() -> None:
    path = hook_path("block-external-writeback.sh")
    assert path.exists(), f"hook_path did not resolve to a real file: {path}"
    assert path.name == "block-external-writeback.sh"
    # Must land under the worktree's .claude/hooks/.
    assert ".claude/hooks" in str(path).replace("\\", "/")
