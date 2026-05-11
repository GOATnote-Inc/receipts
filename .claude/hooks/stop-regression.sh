#!/usr/bin/env bash
# H2 Stop hook: gate session-end on pytest GREEN + pass^k >= 0.95 + kappa >= 0.40.
#
# Stdin payload (Claude Code Stop):
#   {"hook_event_name": "Stop", "stop_hook_active": false, ...}
# Stdin is read and discarded -- the gate logic is purely repo state.
#
# Env knobs:
#   RECEIPTS_STOP_HOOK_DISABLE=1   short-circuit exit 0 (kill switch).
#   RECEIPTS_STOP_SKIP_PYTEST=1    skip `make test` stage (used by unit tests
#                                  for this hook to avoid recursive pytest).
#   RECEIPTS_PASSK_INPUT=<path>    override pass^k fixture path. Default:
#                                  fixtures/regression/passk_results.jsonl
#   RECEIPTS_KAPPA_INPUT=<path>    override kappa fixture path. Default:
#                                  fixtures/regression/kappa_pairs.jsonl
#
# Exit codes:
#   0 -- all enabled gates passed (or fixtures missing => gate is no-op)
#   2 -- a gate failed; STOP-GATE message on stderr

set -u

# Drain stdin; Stop hook ignores payload content.
cat >/dev/null 2>&1 || true

if [ "${RECEIPTS_STOP_HOOK_DISABLE:-}" = "1" ]; then
  exit 0
fi

repo_root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$repo_root" || { printf 'STOP-GATE: cannot cd to repo root\n' >&2; exit 2; }

# ---- Stage 1: pytest ------------------------------------------------------
if [ "${RECEIPTS_STOP_SKIP_PYTEST:-}" != "1" ]; then
  make test
  rc=$?
  # pytest exit 5 = "no tests collected"; treat as no-op pass per Makefile convention.
  if [ "$rc" -ne 0 ] && [ "$rc" -ne 5 ]; then
    printf 'STOP-GATE: pytest failed (exit %s)\n' "$rc" >&2
    exit 2
  fi
fi

# ---- Stage 2: pass^k ------------------------------------------------------
passk_path="${RECEIPTS_PASSK_INPUT:-fixtures/regression/passk_results.jsonl}"
if [ -f "$passk_path" ]; then
  if ! python scripts/verify_passk.py --input "$passk_path" --threshold 0.95 --k 5; then
    printf 'STOP-GATE: pass^k below 0.95\n' >&2
    exit 2
  fi
fi

# ---- Stage 3: kappa -------------------------------------------------------
kappa_path="${RECEIPTS_KAPPA_INPUT:-fixtures/regression/kappa_pairs.jsonl}"
if [ -f "$kappa_path" ]; then
  if ! python scripts/verify_kappa.py --input "$kappa_path" --threshold 0.40; then
    printf 'STOP-GATE: kappa below 0.40\n' >&2
    exit 2
  fi
fi

exit 0
