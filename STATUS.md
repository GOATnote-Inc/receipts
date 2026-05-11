# Active

## Phase
Phase 0 — substrate. **COMPLETE.** 24/24 tasks merged. 328 passing tests, 1 skipped (V5 ground-truth gate awaits real-LLM drafter from P1-5).

Phase 1 — Engineering Receipts vertical. **COMPLETE.** 9/9 tasks merged. 384 passing tests, 1 skipped (same V5 gate). End-to-end pipeline proven against `fixtures/eng/week_0001` with MagicMocked connectors; markdown byte-stable across runs; merkle chain intact; pass^1 = 1.0 on the stub corpus.

Phase 2 — Clinical Audit Ledger vertical. **NOT YET STARTED.**

## Phase 1 tasks (IDs assigned at creation)
- P1-1 (#25) Linear connector — **MERGED**
- P1-2 (#26) GitHub connector — **MERGED**
- P1-3 (#27) Slack connector — **MERGED**
- P1-4 (#28) Granola connector — **MERGED**
- P1-5 (#29) Real-LLM drafter path — **MERGED**
- P1-6 (#30) Reconciler core — **MERGED**
- P1-7 (#31) Output emitter — **MERGED**
- P1-8 (#32) receipts-eng CLI entrypoint — **MERGED**
- P1-9 (#33) Phase 1 E2E weekly-cycle test — **MERGED**

## Exit criteria (Phase 1 done)
- [x] `receipts-eng run --week fixtures/eng/week_0001` succeeds end-to-end against mocked connector replays
- [x] Merkle chain intact; pass^k ≥ 0.95 on the eng fixture (pass^1 = 1.0 measured); κ ≥ 0.40 deferred to Phase 2 with real-LLM dual-judge (stub path is single-judge)
- [x] Markdown + Linear-comment + Slack-DM outputs generated, byte-stable across two consecutive runs
- [x] All Phase 1 tests green; lint clean; substrate suite still passes (384 passed, 1 skipped)

## Verify command
`make venv && make lint && make test`

## Substrate quick-reference (don't touch unless deliberately)
- `src/receipts/ledger/` — L1 schema, Merkle log, run_log, queries, S3 Object Lock, exports
- `src/receipts/judge/` — kappa, L0/L1/L2, dual-judge, hallucination guard, passk, replay
- `src/receipts/drafter/` — revised-spec drafter + encounter-contract (stub LLM; P1-5 wires real LLM)
- `.claude/hooks/` — block-external-writeback, block-phi-export, stamp-judge-call, stop-regression
- `scripts/` — verify_passk.py, verify_kappa.py, gen_eng_fixture.py, gen_drafter_fixtures.py
- `fixtures/eng/week_0001/` — 30 epics, 200 PRs, 30 meetings, 500 threads with ground-truth drift
