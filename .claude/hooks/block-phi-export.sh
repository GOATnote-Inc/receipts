#!/usr/bin/env bash
# H1 PreToolUse hook: block exports/copies touching clinical PHI paths
# unless RECEIPTS_PHI_REASON is set to a non-empty audit reason.
#
# Stdin payload (Claude Code PreToolUse):
#   {"tool_name": "Bash", "tool_input": {"command": "<cmd>"}}
# Exit codes:
#   0 — allow
#   2 — BLOCK

set -u

payload=$(cat)
command=$(printf '%s' "$payload" | jq -r '.tool_input.command // ""' 2>/dev/null || printf '')

if [ -z "$command" ]; then
  exit 0
fi

# Detect bulk-copy/archive tools touching either clinical path.
if printf '%s' "$command" | grep -E -q '(tar|zip|cp|rsync)[[:space:]].*\b(fixtures/clinical|src/receipts/clinical)\b'; then
  if [ -n "${RECEIPTS_PHI_REASON:-}" ]; then
    exit 0
  fi
  printf 'BLOCKED: PHI export to clinical fixtures/src requires RECEIPTS_PHI_REASON env (non-empty)\n' >&2
  printf '  command: %s\n' "$command" >&2
  exit 2
fi

exit 0
