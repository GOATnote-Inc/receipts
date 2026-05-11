# receipts

Append-only intent-vs-execution attestation ledger for AI-mediated workflows. Audit-grade lineage with a Merkle hash chain, κ-graded dual-judge agreement, and regulatory-format exports (FHIR R4 Bundle / SARIF / Markdown / CSV).

Two verticals share the same substrate:

- **Engineering Receipts** — VP Eng-facing scope-drift detection across Linear, GitHub, Slack, and meeting decisions. Catches the standup nobody attends: shipped PRs that drifted from the originally specified epic.
- **Clinical Audit Ledger** — CMIO-facing attestation for AI-mediated clinical workflows. Sits beside ambient scribes (Ambience, Abridge, DAX) and writes FHIR R4 AttestationExtensions on every committed note.

## Quick start

```
make venv
make test
```

441 tests, ruff-clean, hermetic suite (no live API calls — MagicMock injection + record/replay store).

## Engineering-receipts weekly cycle

```
python -m receipts.cli.eng run --week-fixture fixtures/eng/week_0001 --dry-run
```

Produces a Markdown PR body, a Linear comment per drifted epic, and a Slack DM to the VP Eng — all behind a `--dry-run` flag and gated by pass^k ≥ 0.95 + κ ≥ 0.40 + Merkle-intact.

## Clinical-audit weekly cycle

```
python -m receipts.cli.clin run --week-fixture fixtures/clinical/week_0001 --dry-run
```

Produces a PHI-redacted CMIO Markdown report and writes an `AttestationExtension` (canonical URL `https://goatnote.dev/receipts/attestation`) onto each committed FHIR `Composition`. The emitter has no Slack handle by design — patient text never leaves the FHIR + Markdown surface.

## Architecture

- `src/receipts/ledger/` — temporal graph (9 eng tables + 3 clinical tables), Merkle hash chain, append-only `run_log`, `LineageQuery`, S3 Object Lock store (6-yr default / 25-yr opt-in), and four regulatory export generators (Markdown / CSV / SARIF v2.1.0 / FHIR R4 Bundle).
- `src/receipts/judge/` — CEIS three-layer scorer (L0 deterministic rules, L1 structural completeness, L2 LLM judge with model+prompt version registry), Cohen's κ + Wilson CI, dual-judge agreement engine (`claude-opus-4-7` + `gpt-5.4-2026-03-05` default), judge-hallucination guard, pass^k regression gate, record/replay store for hermetic tests.
- `src/receipts/drafter/` — revised-spec drafter (engineering) and encounter-contract drafter (clinical), with hand-crafted stub registries for development and LLM-backed paths for production.
- `src/receipts/connectors/` — Linear, GitHub, Slack, Granola (engineering) and Ambience Scribe + FHIR R4 (clinical), all behind injected `httpx.Client` instances so tests stay hermetic.
- `src/receipts/eng/` and `src/receipts/clinical/` — per-vertical reconciler + emitter pipelines.
- `src/receipts/cli/` — `receipts-eng` and `receipts-clin` argparse entrypoints.

## Guarantees

- **Audit-grade lineage**: every external write appends a Merkle attestation row; `verify_chain()` is a regression gate.
- **Hermetic tests**: no live Linear / GitHub / Slack / Granola / Ambience / FHIR / Anthropic / OpenAI calls in CI. Connectors take an injected client; LLM judges use a replay store keyed by `stable_hash(JudgeCall)`.
- **PHI discipline**: the clinical emitter has no Slack parameter, redacts SSN / MRN / DOB / capitalized-name patterns before emitting, and stores artifact bodies as `(content_ref, content_hash)` pairs only.
- **Byte-stable exports**: regulatory outputs are deterministic across re-runs (sort_keys + canonical separators).

## Relationship to other GOATnote work

- [medomni](https://github.com/GOATnote-Inc/medomni) — the medical reasoning stack receipts attests. Every medomni inference (5-tool agent, 4-persona answer, persona-tagged graph path) can be recorded as a `clinical_drift_finding` row plus a Merkle attestation, then written back to the FHIR `Composition` via `FHIRConnector.write_attestation_extension`. receipts is the audit layer that turns medomni's existing Medplum `AuditEvent` + S3 Object Lock pipeline into malpractice-defense-grade evidence with κ-graded judge agreement.
- [openem-corpus](https://github.com/GOATnote-Inc/openem-corpus) — the 370-condition clinical taxonomy underpinning encounter-contract drafting and clinical L0 rules.
- [medimage-corpus](https://github.com/GOATnote-Inc/medimage-corpus) — the imaging training-data registry powering medomni's image-aware variant; receipts attests image-finding reads the same way it attests text-note reads.

## License

Apache 2.0 — see [LICENSE](LICENSE).
