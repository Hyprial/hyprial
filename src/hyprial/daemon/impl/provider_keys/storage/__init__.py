"""Owner-scope storage for provider-key records: port, local/OrgFS adapters, gate and receipts."""
from __future__ import annotations

from .local import LocalOwnerStore, LocalSpaceResolver
from .port import (
    GRANTS_ROOT,
    KEYS_ROOT,
    UNAVAILABLE_RESOLVER,
    FixedSpaceResolver,
    InvalidSegment,
    OwnerSpaceResolver,
    OwnerStore,
    StaleWrite,
    check_store_path,
    envelope_dek_version,
    grant_id_from_name,
    grant_path,
    grants_dir,
    key_dir,
    key_envelope_path,
    key_meta_path,
)
from .orgfs import OrgFsOwnerStore
from .gate import authority
from .receipts import RECEIPT_FIELDS, InvalidReceipt, ReceiptLogUnavailable, append_receipt, last_used

__all__ = [
    "RECEIPT_FIELDS", "InvalidReceipt", "ReceiptLogUnavailable", "append_receipt", "authority", "last_used",
    "GRANTS_ROOT", "KEYS_ROOT", "UNAVAILABLE_RESOLVER", "FixedSpaceResolver", "InvalidSegment",
    "LocalOwnerStore", "LocalSpaceResolver", "OrgFsOwnerStore",
    "OwnerSpaceResolver", "OwnerStore", "StaleWrite", "check_store_path", "envelope_dek_version",
    "grant_id_from_name", "grant_path", "grants_dir", "key_dir", "key_envelope_path", "key_meta_path",
]
