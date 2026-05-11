# Active

## Phase
Phase 0 — substrate. **COMPLETE.** 24/24 tasks merged.

Phase 1 — Engineering Receipts vertical. **COMPLETE.** 9/9 tasks merged. 384 passing tests, 1 skipped. End-to-end pipeline proven against `fixtures/eng/week_0001` with MagicMocked connectors; markdown byte-stable; Merkle intact; pass^1 = 1.0.

Phase 2 — Clinical Audit Ledger vertical. **COMPLETE.** 9/9 tasks merged. 441 passing tests, 1 skipped. End-to-end pipeline proven against `fixtures/clinical/week_0001` with MagicMocked Scribe + FHIR connectors; markdown byte-stable; Merkle intact across 30 attestations; pass^1 = 1.0; PHI scrub verified on the emitted body; CLI dry-run exit 0 with a canonical summary.

## Phase 2 tasks (all merged)
- P2-1 (#34) Clinical schema extensions (encounter / clinical_artifact / clinical_drift_finding + alembic 0002_clinical) — **merged**
- P2-2 (#35) ScribeConnector (interface + Ambience impl) — **merged**
- P2-3 (#36) FHIRConnector (read Composition + write attestation extension) — **merged**
- P2-4 (#37) LLM-backed clinical drafter path (analog of P1-5 for unknown ENC IDs) — **merged**
- P2-5 (#38) Synthetic clinical encounter fixture generator + clinical-week_0001 — **merged**
- P2-6 (#39) Clinical reconciler core — **merged**
- P2-7 (#40) Clinical PHI-aware output emitter (FHIR Bundle + Markdown + PDF; NO Slack PHI) — **merged**
- P2-8 (#41) receipts-clin CLI entrypoint — **merged**
- P2-9 (#42) Phase 2 E2E test — **merged**

## Exit criteria (Phase 2 done)
- `receipts-clin run --week-fixture fixtures/clinical/week_0001 --dry-run` succeeds end-to-end with mocked Scribe + FHIR connectors
- Merkle chain intact; pass^k ≥ 0.95 on clinical fixture; κ ≥ 0.40 across dual-judge runs (real-LLM path)
- FHIR R4 Bundle output validated; PHI-aware emitter NEVER sends patient text to Slack
- Hallucination flag rate ≤ 5% on stub citations
- All P2 tests green; lint clean; substrate + Phase 1 suites unchanged (384+ passed)

## Hard rules — clinical specific (additions)
- **NEVER store plaintext patient IDs in DB.** `encounter.patient_id_hash` only. Re-identification via separate (out-of-scope) mapping store.
- **NEVER post PHI to Slack DMs.** PHI-aware emitter routes everything sensitive to FHIR write-back + Markdown PR + PDF in-place.
- **Audio + note content** stored as `content_ref` (path) + `content_hash` only. Bodies go to L5 ObjectLockStore on HIPAA-compliant bucket.

## Verify command
`make venv && make lint && make test`

## Substrate quick-reference (don't touch unless deliberately)
- `src/receipts/ledger/` — L1 schema (eng tables shipped; P2-1 adds clinical tables), L2 Merkle, L3 run_log, L4 queries, L5 Object Lock, L6 exports (incl. FHIR R4 Bundle)
- `src/receipts/judge/` — kappa, L0/L1/L2, dual-judge, hallucination guard, passk, replay
- `src/receipts/drafter/` — revised-spec drafter (eng) + encounter-contract drafter (clinical, ENC-001..030 stub) + validator
- `src/receipts/connectors/` — Linear, GitHub, Slack, Granola (eng); P2-2/P2-3 add Scribe + FHIR (clinical)
- `src/receipts/eng/` — reconciler + emitter + CLI (Phase 1)
- `src/receipts/clinical/` — P2 work lands here (reconciler + emitter)
- `src/receipts/cli/` — eng CLI shipped; P2-8 adds clin CLI
- `.claude/hooks/` — block-external-writeback, block-phi-export, stamp-judge-call, stop-regression
- `fixtures/eng/week_0001/` — eng fixture
- `fixtures/clinical/week_0001/` — P2-5 generates this
