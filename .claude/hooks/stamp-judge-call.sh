#!/usr/bin/env bash
# H1 PostToolUse hook: stamp every receipts.judge.l2 invocation into
# .claude/hooks/judge_call_log.jsonl as one append-only JSON line per call.
#
# Stdin payload (Claude Code PostToolUse):
#   {"tool_name": "Bash", "tool_input": {"command": "<cmd>"}, ...}
# Exit codes:
#   0 always — PostToolUse stamps are non-blocking.

set -u

LOG_DIR=".claude/hooks"
LOG_FILE="${LOG_DIR}/judge_call_log.jsonl"

payload=$(cat 2>/dev/null || printf '')
command=$(printf '%s' "$payload" | jq -r '.tool_input.command // ""' 2>/dev/null || printf '')

if [ -z "$command" ]; then
  exit 0
fi

case "$command" in
  *receipts.judge.l2*)
    mkdir -p "$LOG_DIR" 2>/dev/null || exit 0
    ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    # Truncate to 200 chars to avoid log bloat / accidental secret capture.
    truncated=$(printf '%s' "$command" | cut -c1-200)
    entry=$(jq -c -n --arg ts "$ts" --arg cmd "$truncated" '{ts: $ts, command: $cmd}' 2>/dev/null) || exit 0
    printf '%s\n' "$entry" >> "$LOG_FILE" 2>/dev/null || true
    ;;
esac

exit 0
