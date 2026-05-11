"""receipts.eng — engineering-receipts reconciler.

The reconciler is the per-week entrypoint that bridges the connectors
(Linear / GitHub / Slack / Granola), the temporal-graph substrate
(``receipts.ledger``), the revised-spec drafter (``receipts.drafter``),
and the judge stack (``receipts.judge``). It ingests one fixture week into
the L1 ledger, drafts a RevisedSpec per epic, and optionally appends every
draft to a Merkle log + runs the dual-judge / hallucination-guard gates.
"""

from __future__ import annotations

from receipts.eng.reconciler import ReconcilerResult, reconcile_week

__all__ = ["ReconcilerResult", "reconcile_week"]
