"""Typed durable operation receipts for the M2 bootstrap runtime.

Decision: gui-m2-operation-receipts-20261105.  This package is not registered
with production handlers here.  It persists only public operation state and
process identities; credentials and authorization-guide values are excluded.
"""

from __future__ import annotations

from .journal import (
    JOURNAL_FILENAME,
    JOURNAL_VERSION,
    JournalState,
    OPERATION_STORE_COMMIT_UNCONFIRMED,
    OPERATION_STORE_CORRUPT,
    OPERATION_STORE_INSECURE,
    OPERATION_STORE_UNVERIFIED,
    OPERATION_STORE_WRITE_FAILED,
    OperationReceiptError,
    journal_path,
    prepare_root,
    read_journal,
    write_journal,
)
from .service import (
    ControllerIdentity,
    OPERATION_STORE_IN_USE,
    OperationService,
    RECOVERY_CONTROLLER_GONE,
    RECOVERY_CONTROLLER_UNVERIFIED,
    RECOVERY_PROOF_REQUIRED,
    RECOVERY_SCOPE_MISMATCH,
    RecoveryAuthority,
)

__all__ = [
    "ControllerIdentity",
    "JOURNAL_FILENAME",
    "JOURNAL_VERSION",
    "JournalState",
    "OPERATION_STORE_COMMIT_UNCONFIRMED",
    "OPERATION_STORE_CORRUPT",
    "OPERATION_STORE_INSECURE",
    "OPERATION_STORE_IN_USE",
    "OPERATION_STORE_UNVERIFIED",
    "OPERATION_STORE_WRITE_FAILED",
    "OperationReceiptError",
    "OperationService",
    "RECOVERY_CONTROLLER_GONE",
    "RECOVERY_CONTROLLER_UNVERIFIED",
    "RECOVERY_PROOF_REQUIRED",
    "RECOVERY_SCOPE_MISMATCH",
    "RecoveryAuthority",
    "journal_path",
    "prepare_root",
    "read_journal",
    "write_journal",
]
