# receipts

Intent-vs-execution attestation ledger for AI-mediated workflows: a SHA-256 hash-chained attestation table, a dual-judge agreement engine with Cohen's kappa, and regulatory-format exports (FHIR R4 Bundle / SARIF / Markdown / CSV).

**Status:** research build. Both verticals run end to end against committed fixtures with mocked connectors and a hermetic test suite; neither has run against a live Linear, Slack, Ambience or FHIR endpoint. Not a medical device; not for clinical decision-making. Read "What is and is not guaranteed" before relying on any property of the ledger.

Two verticals share the same substrate:

- **Engineering Receipts** — VP Eng-facing scope-drift detection across Linear, GitHub, Slack, and meeting decisions. Catches the standup nobody attends: shipped PRs that drifted from the originally specified epic.
- **Clinical Audit Ledger** — CMIO-facing attestation for AI-mediated clinical workflows. Sits beside ambient scribes (Ambience, Abridge, DAX) and writes FHIR R4 AttestationExtensions on every committed note.

## Quick start

```
make venv
make test
```

Hermetic suite (no live API calls — MagicMock injection + record/replay store); `pytest -q` prints the current count.

## Engineering-receipts weekly cycle

```
python -m receipts.cli.eng run --week-fixture fixtures/eng/week_0001 --dry-run
```

Produces a Markdown PR body, a Linear comment per drifted epic, and a Slack DM to the VP Eng — all behind a `--dry-run` flag; the CLI evaluates pass^k / kappa / chain-intact thresholds and sets the exit code accordingly.

## Clinical-audit weekly cycle

```
python -m receipts.cli.clin run --week-fixture fixtures/clinical/week_0001 --dry-run
```

Produces a PHI-redacted CMIO Markdown report and writes an `AttestationExtension` (canonical URL `https://goatnote.dev/receipts/attestation`) onto each committed FHIR `Composition`. The emitter has no Slack handle by design — patient text never leaves the FHIR + Markdown surface.

## Architecture

- `src/receipts/ledger/` — temporal graph (9 eng tables + 3 clinical tables), SHA-256 hash chain, append-only `run_log`, `LineageQuery`, S3 Object Lock store (6-yr default / 25-yr opt-in), and four regulatory export generators (Markdown / CSV / SARIF v2.1.0 / FHIR R4 Bundle).
- `src/receipts/judge/` — CEIS three-layer scorer (L0 deterministic rules, L1 structural completeness, L2 LLM judge with model+prompt version registry), Cohen's κ + Wilson CI, dual-judge agreement engine (`claude-opus-4-7` + `gpt-5.4-2026-03-05` default), judge-hallucination guard, pass^k regression gate, record/replay store for hermetic tests.
- `src/receipts/drafter/` — revised-spec drafter (engineering) and encounter-contract drafter (clinical), with hand-crafted stub registries for development and LLM-backed paths for production.
- `src/receipts/connectors/` — Linear, GitHub, Slack, Granola (engineering) and Ambience Scribe + FHIR R4 (clinical), all behind injected `httpx.Client` instances so tests stay hermetic.
- `src/receipts/eng/` and `src/receipts/clinical/` — per-vertical reconciler + emitter pipelines.
- `src/receipts/cli/` — `receipts-eng` and `receipts-clin` argparse entrypoints.

## What is and is not guaranteed

- **Hash chain, not a Merkle tree**: `MerkleLog` is a linear SHA-256 chain (`hash = sha256(canonical_json(payload) + prev_hash)`). `verify_chain()` detects an in-place edit of a row's `payload` or `prev_hash` that was not recomputed. It does **not** detect a rewrite that recomputes the chain, truncation of the newest rows, or edits to the `kind` / `target_id` / `created_at` columns (they are not hashed). There is no signing, no external anchoring, and no database-level append-only enforcement; "append-only" is a code convention. Treat the ledger as a change-detection aid, not as tamper-proof evidence.
- **Ledger lifetime**: the shipped CLIs default to a SQLite database created in a `TemporaryDirectory` that is deleted when the run ends. Pass `--db-url` to keep a ledger. The S3 Object Lock store (`src/receipts/ledger/object_lock.py`) is exercised by tests but is not wired into either CLI.
- **Attestation coverage**: the clinical reconciler appends one attestation row per contract; the engineering emitter's external writes (PR body, Linear comment, Slack DM) are not yet attested. The FHIR write path refuses to run without real ledger provenance (`AttestationProvenance`: a 64-hex chain hash; model / prompt / judge ids are recorded as `null` when no LLM ran).
- **Judge gates**: Cohen's kappa is computed only when a `DualJudge` is supplied; the shipped CLIs pass `dual_judge=None`, so the kappa gate is exercised in tests, not in the default run.
- **Hermetic tests**: no live Linear / GitHub / Slack / Granola / Ambience / FHIR / Anthropic / OpenAI calls in CI. Connectors take an injected client; LLM judges use a replay store keyed by `stable_hash(JudgeCall)`.
- **PHI discipline**: the clinical emitter has no Slack parameter, redacts SSN / MRN / DOB / capitalized-name patterns before emitting, and stores artifact bodies as `(content_ref, content_hash)` pairs only. The redaction is pattern-based and has not been validated against real clinical text.
- **Byte-stable exports**: regulatory outputs are deterministic across re-runs (sort_keys + canonical separators).

## Relationship to other GOATnote work

- [medomni](https://github.com/GOATnote-Inc/medomni) — the medical reasoning stack receipts attests. Every medomni inference (5-tool agent, 4-persona answer, persona-tagged graph path) can be recorded as a `clinical_drift_finding` row plus a chain attestation, then written back to the FHIR `Composition` via `FHIRConnector.write_attestation_extension`. receipts is intended as the audit layer over medomni's Medplum `AuditEvent` + S3 Object Lock pipeline; that integration is not built yet.
- [openem-corpus](https://github.com/GOATnote-Inc/openem-corpus) — the 370-condition clinical taxonomy underpinning encounter-contract drafting and clinical L0 rules.
- [medimage-corpus](https://github.com/GOATnote-Inc/medimage-corpus) — the imaging training-data registry powering medomni's image-aware variant; receipts attests image-finding reads the same way it attests text-note reads.

## License

Apache 2.0 — see [LICENSE](LICENSE).
