"""Smoke test: the package imports and exposes a version string."""

import receipts


def test_package_imports() -> None:
    assert receipts.__version__
    assert isinstance(receipts.__version__, str)
