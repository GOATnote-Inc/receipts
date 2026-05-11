"""receipts.cli — operator-facing entrypoints.

This package holds the ``receipts-eng`` and ``receipts-clin`` CLIs. Each
module is a thin argparse wrapper over a vertical's reconciler / emitter
so the same code paths the test suite exercises are what operators type
at the shell.
"""

from __future__ import annotations

__all__: list[str] = []
