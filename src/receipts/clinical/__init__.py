"""receipts.clinical — Clinical Audit Ledger reconciler.

P2 analog of ``receipts.eng``. The clinical reconciler bridges the Scribe +
FHIR connectors, the clinical L1 schema (``encounter`` / ``clinical_artifact``
/ ``clinical_drift_finding``), the encounter-contract drafter (S2 stub
registry + S3 LLM fallback), and the judge stack (κ + hallucination guard
+ pass^k). It ingests one fixture week into the L1 ledger, drafts an
``EncounterContract`` per encounter, validates it, and optionally appends
every draft to a Merkle log + runs the dual-judge / hallucination gates.

Companion to ``receipts.eng``: same surface, clinical-specific schema and
drafter dispatch. PHI discipline is preserved at the storage layer — bodies
live on L5 ObjectLockStore; only ``content_ref`` + ``content_hash`` cross
into this DB.
"""

from __future__ import annotations

from receipts.clinical.emitter import (
    ClinicalEmitterResult,
    emit_clinical_outputs,
)
from receipts.clinical.reconciler import (
    ClinicalReconcilerResult,
    reconcile_clinical_week,
)

__all__ = [
    "ClinicalEmitterResult",
    "ClinicalReconcilerResult",
    "emit_clinical_outputs",
    "reconcile_clinical_week",
]
