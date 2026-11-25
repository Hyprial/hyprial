"""``orgfs.*`` MethodSpec family (batches 3 and 4).

The package keeps the direct ``request_catalog/org`` directory inside the
seven-file structural limit while sharing one wire vocabulary between reads
and writes.
"""

from __future__ import annotations

from .read_specs import BATCH3_ORGFS_BINDINGS
from .write_specs import BATCH4_ORGFS_BINDINGS

__all__ = [
    "BATCH3_ORGFS_BINDINGS",
    "BATCH4_ORGFS_BINDINGS",
]
