# Active

## Phase
Phase 0 — substrate. **COMPLETE.** 24/24 tasks merged. 328 passing tests, 1 skipped (V5 ground-truth gate awaits real-LLM drafter from P1-5).

Phase 1 — Engineering Receipts vertical. **STARTING.**

## Phase 1 tasks (IDs assigned at creation)
- P1-1 (#25) Linear connector — claimable
- P1-2 (#26) GitHub connector — claimable
- P1-3 (#27) Slack connector — claimable
- P1-4 (#28) Granola connector — claimable
- P1-5 (#29) Real-LLM drafter path — claimable (uses J4 LLMJudge + J7 ReplayStore)
- P1-6 (#30) Reconciler core — blockedBy [25, 26, 27, 28, 29]
- P1-7 (#31) Output emitter — blockedBy [25, 26, 27, 30]
- P1-8 (#32) receipts-eng CLI entrypoint — blockedBy [30, 31]
- P1-9 (#33) Phase 1 E2E weekly-cycle test — blockedBy [25–32]

## Exit criteria (Phase 1 done)
- `receipts-eng run --week fixtures/eng/week_0001` succeeds end-to-end against mocked connector replays
- Merkle chain intact; pass^k ≥ 0.95 on the eng fixture; κ ≥ 0.40 across dual-judge runs
- Markdown + Linear-comment + Slack-DM outputs generated, byte-stable across two consecutive runs
- All Phase 1 tests green; lint clean; substrate suite still passes (328+)

## Verify command
`make venv && make lint && make test`

## Substrate quick-reference (don't touch unless deliberately)
- `src/receipts/ledger/` — L1 schema, Merkle log, run_log, queries, S3 Object Lock, exports
- `src/receipts/judge/` — kappa, L0/L1/L2, dual-judge, hallucination guard, passk, replay
- `src/receipts/drafter/` — revised-spec drafter + encounter-contract (stub LLM; P1-5 wires real LLM)
- `.claude/hooks/` — block-external-writeback, block-phi-export, stamp-judge-call, stop-regression
- `scripts/` — verify_passk.py, verify_kappa.py, gen_eng_fixture.py, gen_drafter_fixtures.py
- `fixtures/eng/week_0001/` — 30 epics, 200 PRs, 30 meetings, 500 threads with ground-truth drift
