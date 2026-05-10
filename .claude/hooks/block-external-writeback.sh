#!/usr/bin/env bash
# H1 PreToolUse hook: block writes to external Linear/GitHub/Slack endpoints
# unless RECEIPTS_APPROVAL_TOKEN equals the MVP sentinel RECEIPTS_HOOK_ALLOW.
#
# Stdin payload (Claude Code PreToolUse):
#   {"tool_name": "Bash", "tool_input": {"command": "<cmd>"}}
# Exit codes:
#   0 — allow
#   2 — BLOCK (stderr surfaced to the agent)

set -u

SENTINEL="RECEIPTS_HOOK_ALLOW"

payload=$(cat)
command=$(printf '%s' "$payload" | jq -r '.tool_input.command // ""' 2>/dev/null || printf '')

# Empty command or non-Bash tool: allow.
if [ -z "$command" ]; then
  exit 0
fi

# Detect external-writeback shape: a write-capable client AND an external host.
if printf '%s' "$command" | grep -E -q '(curl|wget|gh|http|httpie)[[:space:]].*\b(linear\.app|api\.github\.com|slack\.com|hooks\.slack\.com)\b'; then
  if [ "${RECEIPTS_APPROVAL_TOKEN:-}" = "$SENTINEL" ]; then
    exit 0
  fi
  printf 'BLOCKED: external write-back to Linear/GitHub/Slack requires RECEIPTS_APPROVAL_TOKEN=%s\n' "$SENTINEL" >&2
  printf '  command: %s\n' "$command" >&2
  exit 2
fi

exit 0
