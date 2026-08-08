"""
KiCad-facing backends.

Only this package may import ``pcbnew``. ``multiboard.core`` is kept free of it
so the index, search, reconciliation, and CLI run anywhere.
"""

from .base import ApplyResult, Backend, BlockSpec, get_backend

__all__ = ["ApplyResult", "Backend", "BlockSpec", "get_backend"]
