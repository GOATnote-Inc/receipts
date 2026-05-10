# receipts — agent operating charter

## What this repo is
Append-only intent-vs-execution attestation ledger. Two products share substrate:
- **Engineering Receipts** — VP Eng-facing scope-drift detection for Linear / GitHub / Slack / meetings
- **Clinical Audit Ledger** — CMIO-facing AI-clinical-workflow attestation

## Start-of-session protocol
1. `cd /Users/kiteboard/receipts`
2. Read STATUS.md (active task + exit criteria)
3. `make venv && make test` — green starting state required

## Hard rules
- NEVER read `.env`, `*.env` files — user-level hooks block; respect them
- NEVER `git add -A` / `git add .` / `git add --all` — stage by name
- TDD discipline: failing test committed in PR before any implementation
- Stop hook gates (when H2 lands): pytest GREEN + pass^k ≥ 0.95 + κ ≥ 0.40
- Every external write to Linear / GitHub / Slack / EHR appends to Merkle ledger (after L2)
- Judge invocations stamp model + prompt-SHA + timestamp into ledger (after J4)

## Architecture
- `src/receipts/ledger/` — temporal graph + Merkle log + run_log (L team)
- `src/receipts/judge/` — CEIS L0/L1/L2 + κ + dual-judge (J team)
- `src/receipts/drafter/` — revised-spec + encounter-contract (S team)
- `src/receipts/connectors/` — MCP shims per vendor (C team)
- `src/receipts/cli/` — receipts-eng + receipts-clin entrypoints

## Reuse map (port + evolve, no runtime import)
- `lostbench/ceis/` → `judge/{l0,l1,l2}.py`
- `healthcraft/{evaluator,sprint_contract,planner}.py` → `drafter/`
- `scribegoat2/experiments/run_log.jsonl` pattern → `ledger/run_log.py`
- `healthcraft/audit_log` → `ledger/merkle.py`
- `healthcraft/V9 κ analysis` → `judge/kappa.py`

## Verify command
`make venv && make lint && make test`
